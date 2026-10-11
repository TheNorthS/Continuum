import asyncio
import hashlib
import logging
import re
from contextlib import nullcontext

from app.common.errors import AppError


class KnowledgeService:
    def __init__(self, settings, repository, processor, embedder, store, cache):
        self.settings, self.repository, self.processor = settings, repository, processor
        self.embedder, self.store, self.cache = embedder, store, cache
        self.ingest_gate = asyncio.Semaphore(2)
        self.graph = None
        self.tracing = None
        self.reranker = None

    async def ingest(self, filename, content, base_id="legacy"):
        filename = re.split(r"[/\\]", filename or "document.md")[-1].strip()
        if not filename or len(filename) > 240:
            raise AppError("invalid_filename", "文件名为空或过长", 422)
        if not filename.lower().endswith((".md", ".markdown", ".txt")):
            raise AppError("unsupported_file", "仅支持 UTF-8 Markdown 和 TXT 文件", 415)
        if len(content) > self.settings.max_upload_bytes:
            raise AppError("upload_too_large", "文件超出上传大小限制", 413)
        async with self.ingest_gate:
            content_digest = hashlib.sha256(content).hexdigest()
            # Preserve legacy hashes so migration does not duplicate already imported files.
            digest = (
                content_digest
                if base_id == "legacy"
                else hashlib.sha256((base_id + ":" + content_digest).encode()).hexdigest()
            )
            existing = await asyncio.to_thread(self.repository.document_by_digest, digest, base_id)
            if existing:
                return {**existing, "duplicate": True}
            parsed = await self.processor.parse(filename, content)
            row, created = await asyncio.to_thread(
                self.repository.reserve_document, filename, digest, base_id
            )
            if not created:
                return {**row, "duplicate": True}
            try:
                vectors = await self.embedder.embed([c["meta"]["title"] + "\n" + c["text"] for c in parsed])
                chunks = [
                    {
                        **c,
                        "meta": {**c["meta"], "document_id": row["id"], "base_id": base_id},
                        "embedding": vector,
                        "id": f"{row['id']}_{index}",
                        "document_id": row["id"],
                    }
                    for index, (c, vector) in enumerate(zip(parsed, vectors, strict=True))
                ]
                await asyncio.to_thread(self.store.upsert, chunks)
                await asyncio.to_thread(self.repository.commit_document, row["id"], chunks)
            except BaseException:
                # SQL status is authoritative: partially indexed chunks are never served.
                await asyncio.shield(asyncio.to_thread(self.repository.fail_document, row["id"]))
                try:
                    await asyncio.shield(asyncio.to_thread(self.store.delete, row["id"]))
                except Exception:
                    logging.getLogger(__name__).warning(
                        "partial_index_cleanup_failed document_id=%s", row["id"]
                    )
                raise
            graph_status = "disabled"
            if self.graph and self.graph.enabled:
                try:
                    await self.graph.index(row["id"])
                    graph_status = "ready"
                except Exception:
                    graph_status = "failed"
                    logging.getLogger(__name__).warning("graph_index_failed document_id=%s", row["id"])
            return {
                **row,
                "status": "ready",
                "chunk_count": len(chunks),
                "duplicate": False,
                "graph_status": graph_status,
            }

    async def delete(self, document_id):
        await asyncio.to_thread(self.repository.mark_deleting, document_id)
        if self.graph:
            await self.graph.delete(document_id)
        await asyncio.to_thread(self.store.delete, document_id)
        await asyncio.to_thread(self.repository.delete_document, document_id)

    async def search(self, query, top_k):
        context = self.tracing.span("retrieval") if self.tracing else nullcontext()
        async with context:
            return await self._search(query, top_k)

    async def _search(self, query, top_k):
        reranking = self.reranker is not None and self.reranker.enabled
        candidates = max(top_k, self.settings.rerank_candidates) if reranking else top_k
        results = await self.candidates(query, candidates)
        if reranking:
            results = await self.reranker.rank(query, results)
            # Re-check after model I/O and cached scores: deletion/revocation may happen in flight.
            ready = await asyncio.to_thread(self.repository.filter_ready, [r["id"] for r in results])
            results = [r for r in results if r["id"] in ready]
        return results[:top_k]

    async def candidates(self, query, candidates):
        """The same ACL-filtered, fused candidate pool used by search and paired evaluation."""
        revision = await asyncio.to_thread(self.repository.revision)
        signature = f"v2:{revision}:{self.settings.vector_backend}:{self.settings.embedding_model}:{query}:{candidates}"
        key = "rag-e:secure:retrieval:" + hashlib.sha256(signature.encode()).hexdigest()

        async def loader():
            vector = (await self.embedder.embed([query]))[0]
            return await asyncio.to_thread(self.store.search, query, vector, candidates * 2)

        async def graph_recall():
            if not self.graph or not self.graph.enabled:
                return []
            try:
                return await self.graph.recall(query, candidates)
            except Exception:
                logging.getLogger(__name__).warning("graph_retrieval_unavailable")
                return []

        results, graph_results = await asyncio.gather(self.cache.get_or_load(key, loader), graph_recall())
        merged, scores = {}, {}
        for channel in (results, graph_results):
            for rank, item in enumerate(channel, 1):
                merged[item["id"]] = item
                scores[item["id"]] = scores.get(item["id"], 0) + 1 / (60 + rank)
        results = [
            {**merged[cid], "score": score}
            for cid, score in sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        ]
        ready = await asyncio.to_thread(self.repository.filter_ready, [r["id"] for r in results])
        return [r for r in results if r["id"] in ready][:candidates]
