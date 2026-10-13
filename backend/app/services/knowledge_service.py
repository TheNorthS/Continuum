import asyncio
import hashlib
import logging
import re
from contextlib import nullcontext

from app.common.errors import AppError


def fuse_candidates(hybrid, graph):
    """Preserve a single ranked channel; apply RRF only when both channels have results."""
    channels = [(name, items) for name, items in (("hybrid", hybrid), ("graph", graph)) if items]
    multi_channel = len(channels) > 1
    merged, scores = {}, {}
    for name, channel in channels:
        seen = set()
        for item in channel:
            cid = item["id"]
            if cid in seen:
                continue
            seen.add(cid)
            rank = len(seen)
            if cid not in merged:
                merged[cid] = {
                    **item,
                    "metadata": {
                        **item.get("metadata", {}),
                        "retrieval_channels": [],
                        "retrieval_details": {},
                    },
                }
            metadata = merged[cid]["metadata"]
            metadata["retrieval_channels"].append(name)
            details = {"rank": rank, "score": item.get("score")}
            if name == "graph" and "graph_recall" in item.get("metadata", {}):
                details["graph_recall"] = dict(item["metadata"]["graph_recall"])
            metadata["retrieval_details"][name] = details
            if multi_channel:
                scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
    if not multi_channel:
        return list(merged.values())
    return [
        {**merged[cid], "score": score}
        for cid, score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


class KnowledgeService:
    def __init__(self, settings, repository, processor, embedder, store, cache):
        # repository	管理 SQL 中的文档记录、状态、Chunk 元数据和版本号
        # processor	解析 Markdown/TXT 并生成文本块
        # embedder	将文本编码为向量
        # store	存储和检索向量及相关内容
        # cache	缓存检索候选结果
        # graph	可选的知识图谱索引与检索
        # reranker	对初步召回的候选进行相关性重排序
        # tracing	记录检索链路的追踪信息
        self.settings, self.repository, self.processor = settings, repository, processor
        self.embedder, self.store, self.cache = embedder, store, cache
        # 它创建了一个初始计数为 2 的异步信号量
        # 同时最多允许两个协程进入这个临界区。
        self.ingest_gate = asyncio.Semaphore(2)
        self.graph = None
        self.tracing = None
        self.reranker = None

    # 文档入库的完整执行流程
    # filename	上传文件名
    # content	原始文件字节内容
    # base_id	知识库标识
    # legacy	默认的历史知识库标识
    async def ingest(self, filename, content, base_id="legacy"):
        # 提取文件名
        filename = re.split(r"[/\\]", filename or "document.md")[-1].strip()
        if not filename or len(filename) > 240:
            raise AppError("invalid_filename", "文件名为空或过长", 422)
        # 校验文档类型
        if not filename.lower().endswith((".md", ".markdown", ".txt")):
            raise AppError("unsupported_file", "仅支持 UTF-8 Markdown 和 TXT 文件", 415)
        # 校验文档大小
        if len(content) > self.settings.max_upload_bytes:
            raise AppError("upload_too_large", "文件超出上传大小限制", 413)
        async with self.ingest_gate:
            # SHA-256 会对整个文件字节内容计算一个 256-bit 摘要，并使用 64 个十六进制字符表示
            content_digest = hashlib.sha256(content).hexdigest()
            # Preserve legacy hashes so migration does not duplicate already imported files.
            # 非 legacy 知识库的 digest 采用，相同的文档可以上传到不同的知识库
            digest = (
                content_digest
                if base_id == "legacy"
                else hashlib.sha256((base_id + ":" + content_digest).encode()).hexdigest()
            )
            # 查找已有记录
            existing = await asyncio.to_thread(self.repository.document_by_digest, digest, base_id)
            if existing:
                return {**existing, "duplicate": True}
            # 执行文档解析
            parsed = await self.processor.parse(filename, content)
            # 文档记录入库
            row, created = await asyncio.to_thread(
                self.repository.reserve_document, filename, digest, base_id
            )
            if not created:
                return {**row, "duplicate": True}
            try:
                # 拼接标题和正文进行向量化
                vectors = await self.embedder.embed([c["meta"]["title"] + "\n" + c["text"] for c in parsed])
                # Chunks信息组装
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
                # 向量入库
                await asyncio.to_thread(self.store.upsert, chunks)
                # 更改关系型数据库中文档的状态
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
        # 单路去重并保留分数；双路执行 RRF，保留来源，同分按片段 ID 稳定排序。
        results = fuse_candidates(results, graph_results)
        ready = await asyncio.to_thread(self.repository.filter_ready, [r["id"] for r in results])
        return [r for r in results if r["id"] in ready][:candidates]
