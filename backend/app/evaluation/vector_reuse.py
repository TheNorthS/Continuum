"""Read-only reuse of an earlier evaluation's exact chunks and embeddings."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, literal_column, select
from sqlalchemy.orm import sessionmaker

from app.common.encryption import Cipher
from app.models.entities import Chunk, Document, IndexProfile


def load_seed(directory: Path, dataset, settings, *, matching_only=False):
    # Partial reuse is authenticated per document against SQL's original-content
    # digest below. Loading a 270 MB full-run JSON to reuse 67 documents creates
    # several large string copies and can exhaust Windows commit memory.
    if not matching_only:
        saved = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
        if saved != {"queries": dataset.queries, "corpus": dataset.corpus}:
            raise ValueError("Vector reuse requires the identical fixed dataset")
        del saved
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    previous = manifest["contract"]["configuration"]
    for key in (
        "chunk_size",
        "chunk_overlap",
        "semantic_chunking",
        "embedding_provider",
        "embedding_model",
        "embedding_dimension",
    ):
        if previous[key] != getattr(settings, key):
            raise ValueError("Vector reuse configuration mismatch: " + key)
    mapping_path = directory / "document-map.json"
    if matching_only and (directory / "checkpoint.db").exists():
        uri = quote((directory / "checkpoint.db").as_posix(), safe="/:")
        with closing(sqlite3.connect("file:" + uri + "?mode=ro", uri=True)) as checkpoint:
            mapping = {
                key: json.loads(value)
                for key, value in checkpoint.execute("SELECT id,value FROM progress WHERE kind='documents'")
            }
    else:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if matching_only:
        from app.evaluation.implementation import implementation_record

        pinned = json.loads((directory / "implementation.json").read_text(encoding="utf-8"))
        if pinned != implementation_record():
            raise ValueError("Partial vector reuse requires identical RAG implementation")
        mapping = {key: value for key, value in mapping.items() if key in dataset.corpus}
    elif set(mapping) != set(dataset.corpus):
        raise ValueError("Vector reuse requires a completely indexed source run")
    database_url = previous.get("database", "")
    runtime = (
        Path(database_url.removeprefix("sqlite:///")).parent
        if database_url.startswith("sqlite:///")
        else directory / ".runtime"
    )
    if not (runtime / "evaluation.db").is_file():
        raise ValueError("Source evaluation database missing")
    cipher = Cipher(runtime / "keyring.json", create=False)
    uri = quote((runtime / "evaluation.db").as_posix(), safe="/:")
    engine = create_engine("sqlite:///file:" + uri + "?mode=ro&uri=true")
    engine.dialect.rage_cipher = cipher
    try:
        with sessionmaker(engine)() as session:
            profile = session.get(IndexProfile, "knowledge").signature
            if (
                settings.embedding_provider == "api"
                and profile.get("embedding_base_url") != settings.embedding_base_url
            ):
                raise ValueError("Vector reuse embedding endpoint mismatch")
            payload = {}
            hashes = {}
            for source_id, doc_id in mapping.items():
                document = session.get(Document, doc_id)
                if not document or document.status != "ready":
                    raise ValueError("Source document is not ready")
                original_digest = hashlib.sha256(dataset.corpus[source_id].encode()).hexdigest()
                if document.digest != original_digest:
                    raise ValueError("Source corpus content mismatch (digest)")
                chunks = list(session.scalars(select(Chunk).where(Chunk.document_id == doc_id)))
                chunks.sort(key=lambda c: c.meta.get("chunk_index", 0))
                if len(chunks) != document.chunk_count or not chunks:
                    raise ValueError("Source index is incomplete")
                payload[source_id] = {
                    "filename": document.filename,
                    "digest": document.digest,
                    "chunks": [{"text": c.text, "meta": c.meta, "embedding": c.embedding} for c in chunks],
                }
                hashes[source_id] = hashlib.sha256(
                    json.dumps(
                        [{"text": c.text, "embedding": c.embedding} for c in chunks],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
            return payload, {
                "source_run": directory.name,
                "source_documents": len(payload),
                "source_chunks": sum(len(d["chunks"]) for d in payload.values()),
                "chunk_vector_sha256": hashes,
                "document_embedding_api_calls": 0,
                "mode": "matching_completed_documents" if matching_only else "identical_dataset",
            }
    finally:
        engine.dispose()


async def import_seed(services, record):
    import asyncio

    row, created = await asyncio.to_thread(
        services.repository.reserve_document, record["filename"], record["digest"], "legacy"
    )
    if not created and row["status"] == "ready":
        return row
    if not created:
        await services.knowledge.delete(row["id"])
        row, _ = await asyncio.to_thread(
            services.repository.reserve_document, record["filename"], record["digest"], "legacy"
        )
    chunks = [
        {
            **c,
            "id": f"{row['id']}_{index}",
            "document_id": row["id"],
            "meta": {**c["meta"], "document_id": row["id"], "base_id": "legacy"},
        }
        for index, c in enumerate(record["chunks"])
    ]
    try:
        await asyncio.to_thread(services.store.upsert, chunks)
        await asyncio.to_thread(services.repository.commit_document, row["id"], chunks)
    except BaseException:
        await asyncio.shield(asyncio.to_thread(services.repository.fail_document, row["id"]))
        raise
    return {**row, "status": "ready", "chunk_count": len(chunks)}


def verification_chunk_ids(repository):
    """SQLite's two existing covering indexes avoid random reads of vector pages.

    Match rowids inside one transaction and retain the repository's exact ACL,
    knowledge scope and ready-state predicate. No ID-format assumptions.
    """
    if repository.db.engine.dialect.name != "sqlite":
        return repository.ready_chunk_ids()
    with repository.db.session() as session:
        # Explicit BEGIN gives both SELECTs the same snapshot on sqlite3's legacy
        # transaction mode, which otherwise does not BEGIN for SELECT statements.
        session.connection().exec_driver_sql("BEGIN")
        identities = {rowid: cid for rowid, cid in session.execute(select(literal_column("rowid"), Chunk.id))}
        documents = repository.allowed_documents(session).where(Document.status == "ready")
        allowed_rows = session.scalars(
            select(literal_column("rowid")).select_from(Chunk).where(Chunk.document_id.in_(documents))
        )
        return [identities[rowid] for rowid in allowed_rows]


def verify_milvus(services):
    """Flush/load and compare exact chunk IDs with the SQL authority before any query."""
    store = services.store
    ids_all = verification_chunk_ids(services.repository)
    expected = set(ids_all)
    store.client.flush(store.collection)
    store.client.load_collection(store.collection)
    total = store.client.query(
        store.collection, filter="", output_fields=["count(*)"], consistency_level="Strong"
    )[0]["count(*)"]
    if total != len(expected):
        raise ValueError("Milvus row count differs from the evaluation SQL chunks")
    for start in range(0, len(ids_all), 500):
        ids = ids_all[start : start + 500]
        found = store.client.query(
            store.collection,
            filter="id in " + json.dumps(ids),
            output_fields=["id"],
            consistency_level="Strong",
        )
        if {r["id"] for r in found} != set(ids):
            raise ValueError("Milvus is missing evaluation chunks")
    deadline = time.monotonic() + 1800
    while True:
        index_detail = {
            name: store.client.describe_index(store.collection, name)
            for name in store.client.list_indexes(store.collection)
        }
        if (
            all(
                str(value.get("state")) == "Finished" and int(value.get("pending_index_rows", 0)) == 0
                for value in index_detail.values()
            )
            and index_detail
        ):
            break
        if time.monotonic() >= deadline:
            raise ValueError("Milvus index build did not finish within the evaluation readiness budget")
        time.sleep(1)
    return {
        "collection": store.collection,
        "server_version": store.client.get_server_version(),
        "chunks_verified": total,
        "consistency": "Strong",
        "dense_index": "HNSW/COSINE",
        "sparse_index": "SPARSE_INVERTED_INDEX/BM25",
        "hnsw_m": services.settings.milvus_hnsw_m,
        "hnsw_ef_construction": services.settings.milvus_hnsw_ef_construction,
        "hnsw_ef_configured": services.settings.milvus_hnsw_ef,
        "hnsw_ef_effective_for_candidates": max(
            services.settings.milvus_hnsw_ef, services.settings.rerank_candidates * 8
        ),
        "index_detail": index_detail,
    }
