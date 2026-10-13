import asyncio
import hashlib

from app.common.errors import AppError
from app.knowledge.graph.extraction import entity_id, grounded, normalized
from app.knowledge.graph.neo4j_store import Neo4jStore

EDGE = "{id:r.id,source:startNode(r).id,target:endNode(r).id,predicate:r.predicate,evidence:r.evidence,chunk_id:r.chunk_id,document_id:r.document_id,filename:r.filename,extraction:r.extraction}"
NODE = "{id:n.id,name:n.name,kind:n.kind}"


class GraphService:
    def __init__(self, settings, repository, llm, cache, tracing):
        self.settings, self.repository, self.llm, self.tracing = settings, repository, llm, tracing
        self.store = Neo4jStore(settings, cache, tracing)
        self.gate = asyncio.Semaphore(1)

    @property
    def enabled(self):
        return self.settings.graph_enabled

    def require_enabled(self):
        if not self.enabled:
            raise AppError("graph_disabled", "请配置并启用 Neo4j 图谱服务", 503)

    async def start(self):
        if self.enabled:
            await self.store.start()

    async def index(self, document_id):
        try:
            async with asyncio.timeout(self.settings.request_timeout):
                return await self._index(document_id)
        except TimeoutError as exc:
            raise AppError("graph_index_timeout", "图谱抽取超时，请减少文档大小或调整批量配置", 504) from exc

    async def _index(self, document_id):
        self.require_enabled()
        async with self.gate:
            chunks = await asyncio.to_thread(self.repository.document_chunks, document_id, "write")
            base_id = chunks[0]["meta"].get("base_id", "legacy") if chunks else "legacy"
            namespace = self.settings.knowledge_namespace + ":" + base_id
            if len(chunks) > self.settings.graph_extract_max_chunks:
                await asyncio.to_thread(
                    self.repository.graph_status, document_id, "failed", "分块超过图谱抽取上限，请调整配置"
                )
                raise AppError("graph_limit", "分块超过图谱抽取上限，请调整配置", 413)
            await asyncio.to_thread(self.repository.graph_status, document_id, "indexing")
            try:
                async with self.tracing.span("graph_index", metadata={"chunks": len(chunks)}):
                    extracted = []
                    for start in range(0, len(chunks), self.settings.llm_batch_size):
                        batch = chunks[start : start + self.settings.llm_batch_size]
                        extracted.extend(
                            await self.llm.batch("extract_graph", [{"text": c["text"]} for c in batch])
                        )
                    entities, relations = {}, []
                    for chunk, extraction in zip(chunks, extracted, strict=True):
                        valid_entities, valid_relations = await asyncio.to_thread(
                            grounded, extraction, chunk["text"], self.settings.graph_max_entities
                        )
                        for entity in valid_entities:
                            eid = entity_id(namespace, entity.name)
                            entities[eid] = {
                                "id": eid,
                                "name": self.cipher.encrypt(entity.name, "graph.name"),
                                "normalized": self.cipher.blind(normalized(entity.name), "entity"),
                                "kind": self.cipher.encrypt(entity.kind, "graph.kind"),
                            }
                        for relation in valid_relations:
                            source, target = (
                                entity_id(namespace, relation.source),
                                entity_id(namespace, relation.target),
                            )
                            rid = hashlib.sha256(
                                f"{chunk['id']}:{source}:{relation.predicate}:{target}".encode()
                            ).hexdigest()
                            relations.append(
                                {
                                    "id": rid,
                                    "source": source,
                                    "target": target,
                                    "predicate": self.cipher.encrypt(relation.predicate, "graph.predicate"),
                                    "evidence": self.cipher.encrypt(relation.evidence, "graph.evidence"),
                                    "chunk_id": chunk["id"],
                                    "filename": self.cipher.encrypt(
                                        chunk["meta"]["filename"], "graph.filename"
                                    ),
                                    "extraction": self.settings.mode,
                                }
                            )
                    await asyncio.to_thread(self.repository.document_chunks, document_id, "write")
                    await self.store.replace(document_id, list(entities.values()), relations)
                    await asyncio.to_thread(self.repository.graph_status, document_id, "ready")
                    return {
                        "document_id": document_id,
                        "entities": len(entities),
                        "relations": len(relations),
                        "mode": self.settings.mode,
                    }
            except BaseException:
                await asyncio.shield(
                    asyncio.to_thread(
                        self.repository.graph_status, document_id, "failed", "图谱抽取失败，可重新构建"
                    )
                )
                raise

    async def delete(self, document_id):
        if self.enabled:
            async with self.gate:
                await self.store.replace(document_id, [], [])

    async def context(self):
        self.require_enabled()
        documents = await asyncio.to_thread(self.repository.documents)
        ready = [d["id"] for d in documents if d["status"] == "ready" and d["graph_status"] == "ready"]
        return {"ns": self.settings.knowledge_namespace, "ready": ready}, ":".join(sorted(ready))

    def decode_node(self, node):
        return {
            **node,
            "name": self.cipher.decrypt(node["name"], "graph.name"),
            "kind": self.cipher.decrypt(node["kind"], "graph.kind"),
        }

    def decode_edge(self, edge):
        return {
            **edge,
            **{k: self.cipher.decrypt(edge[k], "graph." + k) for k in ("predicate", "evidence", "filename")},
        }

    async def candidate_nodes(self, params, revision):
        rows = await self.store.read(
            "nodes",
            """MATCH (n:RageEntity {namespace:$ns})
            WHERE any(d IN n.documents WHERE d IN $ready)
            RETURN n.id AS id,n.name AS name,n.kind AS kind ORDER BY n.id LIMIT 5001""",
            params,
            revision,
        )
        if len(rows) > 5000:
            raise AppError("graph_scope_limit", "当前授权图谱超过5000实体，请分库查询或使用向量检索", 413)
        return await asyncio.to_thread(lambda: [self.decode_node(n) for n in rows])

    async def network(self, query="", limit=100):
        params, revision = await self.context()
        params.update(query=normalized(query), limit=limit)
        nodes = [
            n
            for n in await self.candidate_nodes(params, revision)
            if normalized(query) in normalized(n["name"])
        ][:limit]
        params["ids"] = [n["id"] for n in nodes]
        edges = await self.store.read(
            "edges",
            f"""MATCH (a:RageEntity)-[r:RAGE_REL {{namespace:$ns}}]->(b:RageEntity)
            WHERE r.document_id IN $ready AND a.id IN $ids AND b.id IN $ids RETURN {EDGE} AS edge LIMIT 500""",
            params,
            revision,
        )
        return {"nodes": nodes, "links": [self.decode_edge(e["edge"]) for e in edges], "limit": limit}

    async def paths(self, source, target, hops=3):
        params, revision = await self.context()
        params.update(source=source, target=target)
        hops = max(1, min(int(hops), 3))
        records = await self.store.read(
            "paths",
            f"""MATCH p=(a:RageEntity {{id:$source,namespace:$ns}})-[:RAGE_REL*1..{hops}]-(b:RageEntity {{id:$target,namespace:$ns}})
            WHERE all(r IN relationships(p) WHERE r.namespace=$ns AND r.document_id IN $ready)
            RETURN [n IN nodes(p) | {NODE}] AS nodes,[r IN relationships(p) | {EDGE}] AS links
            ORDER BY length(p) LIMIT 10""",
            params,
            revision,
        )
        records = [
            {
                "nodes": [self.decode_node(n) for n in p["nodes"]],
                "links": [self.decode_edge(e) for e in p["links"]],
            }
            for p in records
        ]
        inferences = []
        for path in records:
            links, nodes = path["links"], path["nodes"]
            if len(links) > 1 and all(
                link["predicate"] in {"子类于", "subclass_of"}
                and link["source"] == nodes[index]["id"]
                and link["target"] == nodes[index + 1]["id"]
                for index, link in enumerate(links)
            ):
                inferences.append(
                    {
                        "source": nodes[0]["name"],
                        "predicate": "子类于",
                        "target": nodes[-1]["name"],
                        "rule": "子类关系传递性",
                        "evidence": links,
                    }
                )
        return {
            "paths": records,
            "inferences": inferences,
            "interpretation": "一般路径表示关联链；仅有向子类链应用传递规则，结论仍依赖原始证据。",
        }

    async def recall(self, query, limit):
        if not self.enabled or limit <= 0:
            return []
        params, revision = await self.context()
        query = normalized(query)
        if not query or not params["ready"]:
            return []
        params.update(query=query, limit=limit * 4)
        matches = {
            n["id"]: normalized(n["name"])
            for n in await self.candidate_nodes(params, revision)
            if normalized(n["name"]) and normalized(n["name"]) in query
        }
        # Prefer specific entity mentions before the seed budget; IDs only break ties.
        params["seeds"] = sorted(matches, key=lambda nid: (-len(matches[nid]), nid))[:10]
        if not params["seeds"]:
            return []
        hops = self.settings.graph_retrieval_hops
        records = await self.store.read(
            "recall_ranked_v2",
            f"""MATCH (seed:RageEntity {{namespace:$ns}})
            WHERE seed.id IN $seeds AND any(d IN seed.documents WHERE d IN $ready)
            MATCH p=(seed)-[:RAGE_REL*1..{hops}]-(other:RageEntity)
            WHERE all(r IN relationships(p) WHERE r.namespace=$ns AND r.document_id IN $ready)
            UNWIND relationships(p) AS r
            WITH r.chunk_id AS chunk_id, count(DISTINCT seed.id) AS matched_entities,
                 min(length(p)) AS min_hops
            RETURN chunk_id, matched_entities, min_hops
            ORDER BY matched_entities DESC, min_hops ASC, chunk_id ASC LIMIT $limit""",
            params,
            revision,
        )
        if not records:
            return []
        chunks = {
            c["id"]: c
            for c in await asyncio.to_thread(self.repository.chunks_by_ids, [r["chunk_id"] for r in records])
        }
        # SQL IN does not preserve the input ranking. Hydrate in graph order,
        # skipping chunks removed by the current ACL/ready-state check.
        ranked = [(record, chunks[record["chunk_id"]]) for record in records if record["chunk_id"] in chunks]
        return [
            {
                "id": c["id"],
                "text": c["text"],
                "metadata": {
                    **c["meta"],
                    "retrieval": "graph",
                    "graph_recall": {
                        "matched_entities": record["matched_entities"],
                        "min_hops": record["min_hops"],
                    },
                },
                "score": 1 / (60 + i),
            }
            for i, (record, c) in enumerate(ranked, 1)
        ]

    async def close(self):
        await self.store.close()
