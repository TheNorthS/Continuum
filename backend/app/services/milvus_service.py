import json

from app.common.errors import AppError


class MilvusStore:
    def __init__(self, settings, repository=None, cipher=None):
        self.settings = settings
        self.client = None
        self.repository, self.cipher = repository, cipher

    def blind_text(self, value):
        from app.knowledge.rag.embedding import tokenize

        # Equality-preserving HMAC terms allow Milvus BM25 without storing readable source text.
        return " ".join(self.cipher.blind(token, "bm25")[:24] for token in tokenize(value))[:65000]

    def initialize(self):
        from pymilvus import DataType, Function, FunctionType, MilvusClient

        self.client = MilvusClient(
            uri=self.settings.milvus_uri, token=self.settings.milvus_token.get_secret_value(), timeout=20
        )
        name = self.settings.milvus_collection + "_secure"
        self.collection = name
        if self.client.has_collection(name):
            fields = {f["name"]: f for f in self.client.describe_collection(name)["fields"]}
            if (
                not {"id", "document_id", "text", "metadata", "dense", "sparse"}.issubset(fields)
                or int(fields["dense"].get("params", {}).get("dim", 0)) != self.settings.embedding_dimension
            ):
                raise AppError(
                    "index_schema", "Milvus collection 结构不兼容，请使用新的 collection 名称", 503
                )
            self.client.load_collection(name)
            self.migrate_legacy()
            return
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("document_id", DataType.VARCHAR, max_length=32)
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"tokenizer": "standard"},
        )
        schema.add_field("metadata", DataType.JSON)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=self.settings.embedding_dimension)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="text_bm25",
                input_field_names=["text"],
                output_field_names=["sparse"],
                function_type=FunctionType.BM25,
            )
        )
        indexes = self.client.prepare_index_params()
        indexes.add_index(
            "dense",
            index_type="HNSW",
            metric_type="COSINE",
            params={
                "M": self.settings.milvus_hnsw_m,
                "efConstruction": self.settings.milvus_hnsw_ef_construction,
            },
        )
        indexes.add_index(
            "sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"bm25_k1": 1.2, "bm25_b": 0.75},
        )
        self.client.create_collection(name, schema=schema, index_params=indexes, consistency_level="Strong")
        self.migrate_legacy()

    def migrate_legacy(self):
        original = self.settings.milvus_collection
        if not self.client.has_collection(original):
            return
        if self.repository is None or self.cipher is None:
            raise AppError("index_migration", "安全迁移需要业务数据库和密钥", 503)
        expected = {c["id"]: c for c in self.repository.ready_chunks()}
        iterator = self.client.query_iterator(original, batch_size=500, output_fields=["id", "document_id"])
        observed = set()
        try:
            while rows := iterator.next():
                for row in rows:
                    if row["id"] not in expected:
                        raise AppError(
                            "index_migration", "旧索引包含业务数据库之外的数据，请先导出并人工迁移", 503
                        )
                    observed.add(row["id"])
        finally:
            iterator.close()
        self.upsert(list(expected.values()))
        self.client.flush(self.collection)
        if expected:
            count = self.client.query(
                self.collection, filter="", output_fields=["count(*)"], consistency_level="Strong"
            )[0]["count(*)"]
            if count < len(expected):
                raise AppError("index_migration", "安全索引校验未通过，旧索引保留", 503)
            for start in range(0, len(expected), 200):
                ids = list(expected)[start : start + 200]
                verified = self.client.query(
                    self.collection,
                    filter="id in " + json.dumps(ids),
                    output_fields=["id", "metadata"],
                    consistency_level="Strong",
                )
                if {row["id"] for row in verified} != set(ids):
                    raise AppError("index_migration", "安全索引记录校验失败，旧索引保留", 503)
                for row in verified:
                    value = self.cipher.decrypt(row["metadata"]["encrypted"], "milvus.payload:" + row["id"])
                    if value["text"] != expected[row["id"]]["text"]:
                        raise AppError("index_migration", "安全索引正文校验失败，旧索引保留", 503)
        # Only the configured, fully matched legacy index is removed, after encrypted replacement is verified.
        self.client.drop_collection(original)

    def upsert(self, chunks):
        for start in range(0, len(chunks), 64):
            self.client.upsert(
                self.collection,
                data=[
                    {
                        "id": c["id"],
                        "document_id": c["document_id"],
                        "text": self.blind_text(c["text"]),
                        "metadata": {
                            "encrypted": self.cipher.encrypt(
                                {"text": c["text"], "meta": c["meta"]}, "milvus.payload:" + c["id"]
                            )
                        },
                        "dense": c["embedding"],
                    }
                    for c in chunks[start : start + 64]
                ],
            )

    def delete(self, document_id):
        self.client.delete(self.collection, filter="document_id == " + json.dumps(document_id))

    def search(self, query, vector, top_k):
        from pymilvus import AnnSearchRequest, RRFRanker

        allowed = self.repository.ready_document_ids()
        if not allowed:
            return []
        expression = "document_id in " + json.dumps(allowed)

        requests = [
            AnnSearchRequest(
                [vector],
                "dense",
                {"metric_type": "COSINE", "params": {"ef": max(self.settings.milvus_hnsw_ef, top_k * 4)}},
                limit=top_k * 4,
                expr=expression,
            ),
            AnnSearchRequest(
                [self.blind_text(query)],
                "sparse",
                {"metric_type": "BM25", "params": {}},
                limit=top_k * 4,
                expr=expression,
            ),
        ]
        results = self.client.hybrid_search(
            self.collection,
            reqs=requests,
            ranker=RRFRanker(k=60),
            limit=top_k,
            output_fields=["metadata"],
            consistency_level="Strong",
        )
        return [
            {
                "id": str(hit["id"]),
                "score": hit["distance"],
                "text": self.cipher.decrypt(
                    hit["entity"]["metadata"]["encrypted"], "milvus.payload:" + str(hit["id"])
                )["text"],
                "metadata": self.cipher.decrypt(
                    hit["entity"]["metadata"]["encrypted"], "milvus.payload:" + str(hit["id"])
                )["meta"],
            }
            for hit in results[0]
        ]

    def healthy(self):
        self.client.describe_collection(self.collection)

    def close(self):
        if self.client:
            self.client.close()
