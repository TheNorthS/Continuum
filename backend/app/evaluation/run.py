"""Reproducible paired evaluation using the application's real retrieval and chat services."""

import argparse
import asyncio
import gzip
import hashlib
import importlib.metadata
import json
import logging
import re
import time
from collections import Counter, OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from app.common.context import knowledge_scope
from app.common.errors import AppError
from app.config import ROOT
from app.evaluation.checkpoint import (
    Checkpoint,
    RunLock,
    check_capacity,
    progress,
    safe_error,
    safe_error_frames,
)
from app.evaluation.concurrency import DependencyFailureGuard, run_question_workers, worker_chats
from app.evaluation.configuration import load_settings, summary
from app.evaluation.datasets import crud, t2
from app.evaluation.full_data import full_crud
from app.evaluation.implementation import pin_implementation
from app.evaluation.metrics import (
    aggregate,
    answer_metrics,
    answer_population_table,
    answer_populations,
    latency,
    paired_interval,
    retrieval_metrics,
    unique_documents,
)
from app.evaluation.timing import collect_timings, install_timing
from app.evaluation.vector_reuse import import_seed, load_seed, verify_milvus
from app.infrastructure.container import Container
from app.pipeline_profile import effective_pipeline, pipeline_fingerprint


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.suffix == ".gz":
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=3) as stream:
            json.dump(value, stream, ensure_ascii=False)
    else:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(
        gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_text(encoding="utf-8")
    )


class Usage:
    def __init__(self, path):
        self.path = path
        self.totals = read_json(path, {})

    async def response(self, response):
        # Aggregate only numerical usage/status; never record Authorization or provider bodies.
        # A mid-body network failure must reach post_json's transport retry. If it
        # is swallowed here, httpx reads the exhausted stream again and raises
        # StreamConsumed instead of the original retryable timeout/disconnect.
        await response.aread()
        try:
            endpoint = response.request.url.path
            category = (
                "embedding"
                if endpoint.endswith("/embeddings")
                else "rerank"
                if "rerank" in endpoint
                else "llm_and_moderation"
            )
            entry = self.totals.setdefault(category, {"requests": 0, "errors": 0, "usage": {}})
            entry["requests"] += 1
            entry["errors"] += int(response.status_code >= 400)
            payload = response.json()
            usage = payload.get("usage") or payload.get("meta", {}).get("tokens") or {}
            for key, value in usage.items():
                if type(value) in (int, float):
                    entry["usage"][key] = entry["usage"].get(key, 0) + value
            write_json(self.path, self.totals)
        except Exception:
            pass  # Accounting must not turn a valid upstream response into a failed request.


class PairedKnowledge:
    """Only replaces the experiment's selection policy; candidates and safety remain real."""

    def __init__(self, service, cache_directory, candidate_count):
        self.service = service
        self.cache_directory = cache_directory
        self.candidate_count = candidate_count
        self.variant = "rerank"
        self.records = OrderedDict()
        self.locks = [asyncio.Lock() for _ in range(64)]
        cache_directory.mkdir(exist_ok=True)

    def _key(self, query):
        return hashlib.sha256(
            json.dumps([query, knowledge_scope.get(), self.candidate_count]).encode()
        ).hexdigest()

    async def candidates(self, query):
        key = self._key(query)
        async with self.locks[int(key[:8], 16) % len(self.locks)]:
            return await self._candidates(query, key)

    async def _candidates(self, query, key):
        path = self.cache_directory / (key + ".json.gz")
        if not path.exists() and path.with_suffix("").exists():
            path = path.with_suffix("")  # Read old pilot caches without new provider calls.
        if key not in self.records:
            if path.exists():
                self.records[key] = read_json(path, {})
            else:
                started = time.perf_counter()
                items = await self.service.candidates(query, self.candidate_count)
                self.records[key] = {
                    "query": query,
                    "sources": items,
                    "retrieval_seconds": time.perf_counter() - started,
                }
                write_json(path, self.records[key])
            while len(self.records) > 4:
                self.records.popitem(last=False)
        self.records.move_to_end(key)
        return self.records[key], path

    async def ranked(self, query):
        key = self._key(query)
        async with self.locks[int(key[:8], 16) % len(self.locks)]:
            return await self._ranked(query, key)

    async def _ranked(self, query, key):
        record, path = await self._candidates(query, key)
        if "ranked" not in record:
            started = time.perf_counter()
            items = await self.service.reranker.rank(query, record["sources"])
            if any(s.get("metadata", {}).get("rerank_status") == "fallback" for s in items):
                raise AppError("evaluation_rerank_fallback", "精排实验不接受回退结果", 503)
            record.update(ranked=items, rerank_seconds=time.perf_counter() - started)
            write_json(path, record)
        return record

    async def search(self, query, top_k):
        record = await self.ranked(query) if self.variant == "rerank" else (await self.candidates(query))[0]
        sources = record["ranked"] if self.variant == "rerank" else record["sources"]
        ready = await asyncio.to_thread(self.service.repository.filter_ready, [s["id"] for s in sources])
        return [s for s in sources if s["id"] in ready][:top_k]


def make_report(directory, dataset, results, status):
    items = [results[q["id"]] for q in dataset.queries if q["id"] in results]
    variants = ("rrf", "rerank")
    metrics = {
        "status": status,
        "dataset": dataset.name,
        "planned_queries": len(dataset.queries),
        "completed_queries": len(items),
        "full_dataset": bool(dataset.metadata.get("full")),
        "corpus_size": len(dataset.corpus),
        "retrieval_judgments_available": any(q["relevant"] for q in dataset.queries),
        "variants": {},
        "vector_backend": read_json(directory / "manifest.json", {})
        .get("contract", {})
        .get("configuration", {})
        .get("vector_backend", "legacy-local"),
        "pipeline_fingerprint": read_json(directory / "manifest.json", {})
        .get("contract", {})
        .get("configuration", {})
        .get("pipeline_fingerprint"),
    }
    for variant in variants:
        entries = [r[variant] for r in items]
        metrics["variants"][variant] = {
            "retrieval": aggregate([r["retrieval_metrics"] for r in entries]),
            "retrieval_errors": sum(bool(r.get("retrieval_error")) for r in entries),
        }
        answers = [r["answer"] for r in entries if "answer" in r]
        if answers:
            metrics["variants"][variant].update(
                answer=aggregate([a["metrics"] for a in answers]),
                answer_status=dict(Counter(a["status"] for a in answers)),
                chat_latency=latency([a["seconds"] for a in answers]),
                **answer_populations(answers),
            )
    paired = [{v: r[v]["retrieval_metrics"] for v in variants} for r in items]
    metrics["paired_retrieval"] = {
        key: paired_interval(paired, key) for key in ("recall@4", "mrr@10", "ndcg@10")
    }
    metrics["initial_retrieval_latency"] = latency(
        [r["retrieval_seconds"] for r in items if "retrieval_seconds" in r]
    )
    metrics["rerank_incremental_latency"] = latency(
        [r["rerank_seconds"] for r in items if "rerank_seconds" in r]
    )
    metrics["api_usage"] = read_json(directory / "usage.json", {})
    metrics["by_question_concurrency"] = {}
    for concurrency in sorted({r.get("execution", {}).get("question_concurrency", 1) for r in items}):
        selected = [r for r in items if r.get("execution", {}).get("question_concurrency", 1) == concurrency]
        section = {"questions": len(selected), "variants": {}}
        for variant in variants:
            answers = [r[variant]["answer"] for r in selected if "answer" in r[variant]]
            timed = [a for a in answers if "stage_timings" in a]
            stage_names = sorted({key for a in timed for key in a["stage_timings"]})
            section["variants"][variant] = {
                "chat_latency": latency([a["seconds"] for a in answers]),
                "answer_status": dict(Counter(a["status"] for a in answers)),
                "timed_answers": len(timed),
                "stages": {
                    key: {
                        "calls": sum(a["stage_timings"].get(key, {}).get("calls", 0) for a in timed),
                        "total_seconds": sum(
                            a["stage_timings"].get(key, {}).get("seconds", 0) for a in timed
                        ),
                        "per_answer": latency(
                            [a["stage_timings"].get(key, {}).get("seconds", 0) for a in timed]
                        ),
                    }
                    for key in stage_names
                },
            }
        metrics["by_question_concurrency"][str(concurrency)] = section
    metrics["timing_note"] = (
        "Span durations are inclusive and may overlap; do not sum workflow and dependency spans. Historical answers without stage timing are excluded from stage aggregates."
    )
    metrics["graph_index"] = {
        key: value for key, value in read_json(directory / "graph-index.json", {}).items() if key != "network"
    }
    tasks = sorted({r.get("task") for r in items if r.get("task")})
    metrics["by_task"] = {}
    for task in tasks:
        selected = [r for r in items if r.get("task") == task]
        metrics["by_task"][task] = {
            "questions": len(selected),
            **{
                variant: {
                    "retrieval": aggregate([r[variant]["retrieval_metrics"] for r in selected]),
                    "answer": aggregate(
                        [r[variant]["answer"]["metrics"] for r in selected if "answer" in r[variant]]
                    ),
                    **answer_populations([r[variant]["answer"] for r in selected if "answer" in r[variant]]),
                }
                for variant in variants
            },
        }
    write_json(directory / "metrics.json", metrics)
    lines = [
        f"# {dataset.name} 评测报告",
        "",
        f"状态：{status}；完成 {len(items)} / {len(dataset.queries)} 题；语料 {len(dataset.corpus)} 条。",
        f"向量检索后端：{metrics['vector_backend']}。",
        f"共享 RAG 参数指纹：{metrics.get('pipeline_fingerprint') or '历史版本未记录'}。",
        "",
        (
            f"这是当前选择语料上的完整问题集实验，语料分片数 {dataset.metadata.get('corpus_partition_count', '见清单')}；使用本项目分块和问答流程，不等同于官方榜单。"
            if dataset.metadata.get("full")
            else "这是固定语料子集实验，不是完整公开榜单成绩。"
        )
        + "RRF 与 Qwen 精排共享同一 Top-32（或配置值）候选池。",
        "",
        "| 检索指标 | RRF | Qwen 精排 |",
        "|---|---:|---:|",
    ]
    for key in ("recall@1", "recall@4", "recall@10", "recall@32", "mrr@10", "ndcg@10", "precision@4"):
        values = [metrics["variants"][v]["retrieval"].get(key) for v in variants]
        shown = [f"{value:.4f}" if value is not None else "N/A（无完整相关性标注）" for value in values]
        lines.append(f"| {key} | {shown[0]} | {shown[1]} |")
    if items and "answer" in metrics["variants"]["rrf"]:
        lines += ["", *answer_population_table(metrics["variants"])]
        lines += [
            "",
            "| 全量端到端回答指标（文本重合，不是语义准确率） | RRF | Qwen 精排 |",
            "|---|---:|---:|",
        ]
        for key in ("normalized_em", "character_f1", "character_rougeL"):
            values = [metrics["variants"][v]["answer"].get(key, 0) for v in variants]
            lines.append(f"| {key} | {values[0]:.4f} | {values[1]:.4f} |")
        lines += [
            "",
            "全量端到端口径保留拒答、内容拦截和错误，回答重合指标计 0；正常回答题口径将其排除。两种分母分别报告，保留原始回答与标准答案供复核。",
        ]
    if any("answer" in r[v] for r in items for v in variants):
        lines += [
            "",
            "## 问答耗时（按问题并发度分组）",
            "",
            "| 同时运行题数 | 组别 | 完成题数 | 平均秒 | p50 秒 | p95 秒 |",
            "|---:|---|---:|---:|---:|---:|",
        ]
        for concurrency, section in metrics["by_question_concurrency"].items():
            for variant in variants:
                timing = section["variants"][variant]["chat_latency"]
                if timing:
                    lines.append(
                        f"| {concurrency} | {variant} | {section['questions']} | {timing['mean_seconds']:.2f} | {timing['p50_seconds']:.2f} | {timing['p95_seconds']:.2f} |"
                    )
        lines += [
            "",
            "并发只改变评测调度，生产 LLM/精排/审查的共享并发上限不变。阶段计时从启用时开始，旧结果不补造；工作流与内部模型 span 存在包含或并行关系，不能相加当作总耗时。",
        ]
    lines += [
        "",
        "## 范围与限制",
        "",
        "- 复用真实分块、OpenAI Embedding、RRF 和硅基流动 Qwen。CRUD 使用完整 ChatService 安全审查与质量检查。",
        (
            "- 全量模式：不抽样、不为问题额外注入正例；数据源、去重规则和 SHA-256 记录在 manifest。"
            if dataset.metadata.get("full")
            else "- 固定 seed；所有抽中问题的已标注资料入库，并加入随机干扰资料。较小语料会使检索更容易。"
        ),
        (
            f"- CRUD 覆盖三个 QA 任务全部问题与 {dataset.metadata.get('corpus_partition_count', '所选')} 个官方语料分片；缺少全库 qrels，检索指标记 N/A。摘要/续写/幻觉修改任务不属于本次问答评测。"
            if dataset.metadata.get("full")
            else "- CRUD 的来源关联不是完整人工相关性标注；只使用 QA 附带的新闻池，不是 8 万篇全集。"
        ),
        "- ROUGE-L 使用中文字符级 tokenizer，与原项目 jieba 口径不同，不能作为 CRUD 官方排名。",
        "- 本轮未使用 LLM 裁判/Ragas，无法仅凭这些指标判断语义准确率和事实忠实性。",
        "- 配置对齐版的 Qwen 组调用实际 KnowledgeService；RRF 组为共享候选的消融对照。缓存仍会影响计时，不是独立冷启动性能测试。",
        "- 全部报告保存在隔离评测目录；没有读写业务数据库。",
        "",
    ]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    with (directory / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        import csv

        writer = csv.writer(handle)
        writer.writerow(["variant", "category", "metric", "value"])
        for variant in variants:
            for category in ("retrieval", "answer", "answer_answered_only", "answer_population"):
                for key, value in metrics["variants"][variant].get(category, {}).items():
                    if not isinstance(value, dict):
                        writer.writerow([variant, category, key, value])
    with (directory / "queries.jsonl").open("w", encoding="utf-8") as handle:
        for row in items:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (directory / "qrels.tsv").open("w", encoding="utf-8") as qrels:
        for query in dataset.queries:
            for doc_id, relevance in query["relevant"].items():
                qrels.write(f"{query['id']}\t0\t{doc_id}\t{relevance}\n")
    for variant in variants:
        with (directory / f"{variant}.run").open("w", encoding="utf-8") as run:
            for query in dataset.queries:
                for rank, doc_id in enumerate(
                    results.get(query["id"], {}).get(variant, {}).get("ranking", []), 1
                ):
                    run.write(f"{query['id']} Q0 {doc_id} {rank} {1 / rank} {variant}\n")
    return metrics


async def evaluate(args):
    dataset = (
        (t2(full=True) if args.dataset == "t2" else full_crud(include_auxiliary=args.crud_official_directory))
        if args.full
        else ((t2 if args.dataset == "t2" else crud)(args.questions, args.documents, args.seed))
    )
    if args.exact_documents is not None and len(dataset.corpus) != args.exact_documents:
        raise ValueError("Required positive documents exceed the exact corpus budget; reduce question count")
    directory = ROOT / "test-results/evaluation" / args.run_name
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint(directory)
    if args.report_only:
        results = checkpoint.read("results")
        checkpoint.close()
        saved = read_json(directory / "dataset.json", {})
        if saved != {"queries": dataset.queries, "corpus": dataset.corpus}:
            raise ValueError("Report-only arguments must match the saved dataset")
        state = "complete" if len(results) == len(dataset.queries) else "partial"
        make_report(directory, dataset, results, state)
        print(
            json.dumps({"stage": "report_only", "status": state, "path": str(directory / "report.md")}),
            flush=True,
        )
        return
    pin_implementation(directory)
    runtime = ((args.runtime_root / args.run_name) if args.runtime_root else directory / ".runtime").resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    settings = load_settings(args.config, workspace=runtime, backend=args.backend)
    if args.candidates is not None and args.candidates != settings.rerank_candidates:
        raise ValueError(
            "Candidate count must match the shared configuration; edit the profile and use a new run"
        )
    candidate_count = settings.rerank_candidates
    if not settings.rerank_provider == "api":
        raise ValueError("This paired online evaluation requires the configured rerank API")
    configuration = {
        **summary(settings),
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "semantic_chunking": settings.semantic_chunking,
        "candidate_count": candidate_count,
        "top_k_generation": settings.retrieval_top_k,
        "llm_thinking": settings.llm_thinking,
        "moderation_thinking": settings.moderation_thinking,
        "max_rewrites": settings.max_rewrites,
        "max_generations": settings.max_generations,
        "rerank_api_format": settings.rerank_api_format,
        "rerank_failure_mode": settings.rerank_failure_mode,
        "rerank_min_score": settings.rerank_min_score,
        "pipeline": effective_pipeline(settings),
        "pipeline_fingerprint": pipeline_fingerprint(settings),
        "prompt_fingerprint": hashlib.sha256(
            b"".join(
                path.read_bytes()
                for folder in ("prompts", "safety")
                for path in sorted((Path(__file__).parents[1] / folder).glob("*.py"))
            )
        ).hexdigest(),
    }
    if settings.vector_backend == "milvus":
        configuration["milvus_index"] = {
            "type": "HNSW",
            "metric": "COSINE",
            "M": settings.milvus_hnsw_m,
            "efConstruction": settings.milvus_hnsw_ef_construction,
            "ef": settings.milvus_hnsw_ef,
            "sparse": "BM25",
            "consistency": "Strong",
            "endpoint_fingerprint": hashlib.sha256(settings.milvus_uri.encode()).hexdigest(),
        }
    contract = {
        "dataset": dataset.metadata,
        "configuration": configuration,
        "query_ids": [q["id"] for q in dataset.queries],
        "corpus_ids": list(dataset.corpus),
        "schema_version": 2,
        "reuse_vectors_from": args.reuse_vectors_from,
    }
    if args.reuse_matching_vectors_from:
        contract["reuse_matching_vectors_from"] = args.reuse_matching_vectors_from
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        if read_json(manifest_path, {})["contract"] != contract:
            raise ValueError("Run configuration changed; choose a new --run-name")
    else:
        write_json(
            manifest_path,
            {
                "contract": contract,
                "created_at": datetime.now(UTC).isoformat(),
                "packages": {
                    name: importlib.metadata.version(name)
                    for name in ("ir-measures", "rouge-score", "pyarrow")
                },
            },
        )
        if settings.pipeline_profile:
            write_json(
                directory / "pipeline-profile.json", {"version": 1, "settings": effective_pipeline(settings)}
            )
    write_json(directory / "dataset.json", {"queries": dataset.queries, "corpus": dataset.corpus})
    print(
        json.dumps(
            {
                "stage": "prepared",
                "dataset": dataset.name,
                "documents": len(dataset.corpus),
                "questions": len(dataset.queries),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.prepare_only:
        checkpoint.close()
        return
    check_capacity(directory, runtime)
    if settings.pipeline_profile:
        selected = read_json(settings.pipeline_profile, {})
        if selected.get("settings") != effective_pipeline(settings):
            raise ValueError("Effective evaluation pipeline drifted from the shared profile")
    seed = None
    reuse_source = args.reuse_vectors_from or args.reuse_matching_vectors_from
    if reuse_source:
        if reuse_source == args.run_name:
            raise ValueError("Cannot reuse the current run as its own source")
        progress(directory, "loading_reusable_vectors", source_run=reuse_source)
        seed, reuse_record = await asyncio.to_thread(
            load_seed,
            ROOT / "test-results/evaluation" / reuse_source,
            dataset,
            settings,
            matching_only=bool(args.reuse_matching_vectors_from),
        )
        write_json(directory / "vector-reuse.json", reuse_record)
    usage = Usage(directory / "usage.json")
    services = Container(settings)
    services.http.event_hooks["response"].append(usage.response)
    services.moderation.http.event_hooks["response"].append(usage.response)
    results = checkpoint.read("results")
    try:
        progress(directory, "starting_services")
        await services.start()
        mapping = checkpoint.read("documents")
        pending_documents = iter((key, value) for key, value in dataset.corpus.items() if key not in mapping)
        progress(directory, "index", documents=len(mapping), total=len(dataset.corpus))

        async def index_worker():
            for source_id, text in pending_documents:
                check_capacity(directory, runtime)
                record = (
                    await import_seed(services, seed[source_id])
                    if seed and source_id in seed
                    else await services.knowledge.ingest(f"doc-{source_id}.txt", text.encode())
                )
                if record["status"] in {"failed", "indexing"}:
                    # An interrupted import may leave this run's own failed reservation.
                    # Retry only this exact document inside the isolated evaluation store.
                    await asyncio.to_thread(services.repository.fail_document, record["id"])
                    await services.knowledge.delete(record["id"])
                    record = (
                        await import_seed(services, seed[source_id])
                        if seed and source_id in seed
                        else await services.knowledge.ingest(f"doc-{source_id}.txt", text.encode())
                    )
                if record["status"] != "ready":
                    raise ValueError("Evaluation document is not ready")
                mapping[source_id] = record["id"]
                checkpoint.put("documents", source_id, record["id"])
                if len(mapping) % 25 == 0:
                    progress(directory, "index", documents=len(mapping), total=len(dataset.corpus))

        # SQLite has one writer. Serialize evaluation document imports there to
        # avoid read-to-write transaction upgrade contention during long runs.
        # This changes scheduling only; production parsing/models remain shared.
        ingestion_workers = 1 if services.db.engine.dialect.name == "sqlite" else 2
        write_json(
            directory / "runtime-controls.json",
            {
                "ingestion_workers": ingestion_workers,
                "database_dialect": services.db.engine.dialect.name,
                "reason": "single-writer SQLite evaluation scheduling; RAG policy unchanged",
            },
        )
        async with asyncio.TaskGroup() as group:
            for _ in range(ingestion_workers):
                group.create_task(index_worker())
        write_json(directory / "document-map.json", mapping)
        reverse = {value: key for key, value in mapping.items()}
        if settings.vector_backend == "milvus":
            progress(directory, "verifying_index", documents=len(mapping), total=len(dataset.corpus))
            evidence = await asyncio.to_thread(verify_milvus, services)
            write_json(directory / "milvus-index.json", evidence)
            print(json.dumps({"stage": "milvus_verified", **evidence}), flush=True)
        write_json(
            directory / "index.json",
            {
                "documents": len(mapping),
                "chunks": evidence["chunks_verified"]
                if settings.vector_backend == "milvus"
                else len(await asyncio.to_thread(services.repository.ready_chunk_ids)),
            },
        )
        if services.graph.enabled:
            documents = await asyncio.to_thread(services.repository.documents)
            pending = [d for d in documents if d["status"] == "ready" and d["graph_status"] != "ready"]
            graph_errors = {}
            for document in pending:
                try:
                    await services.graph.index(document["id"])
                except Exception as exc:
                    graph_errors[document["id"]] = (
                        exc.code if isinstance(exc, AppError) else type(exc).__name__
                    )
                    # Match the production upload behavior: vector-ready documents stay
                    # searchable, failed graph documents don't silently become graph-ready.
                    print(
                        json.dumps(
                            {
                                "stage": "graph_rebuild_failed",
                                "document_id": document["id"],
                                "error": graph_errors[document["id"]],
                            }
                        ),
                        flush=True,
                    )
            write_json(
                directory / "graph-index.json",
                {
                    "enabled": True,
                    "namespace": settings.knowledge_namespace,
                    "documents": len(documents),
                    "rebuilt": len(pending),
                    "rebuild_errors": graph_errors,
                    "status_counts": dict(
                        Counter(
                            d["graph_status"] for d in await asyncio.to_thread(services.repository.documents)
                        )
                    ),
                },
            )
        paired = PairedKnowledge(services.knowledge, directory / "candidate-cache", candidate_count)
        install_timing(services)
        paired.variant = "rrf"
        admin = await asyncio.to_thread(services.repository.user, username=settings.admin_username)

        def make_worker(worker_id):
            chats = worker_chats(services, paired) if args.dataset == "crud" else {}

            async def process(index, query):
                row = {
                    "id": query["id"],
                    "question": query["question"],
                    "references": query["answers"],
                    "relevant": query["relevant"],
                    "task": query.get("task"),
                    "group": query.get("group"),
                    "execution": {
                        "question_concurrency": args.query_concurrency,
                        "worker": worker_id,
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                }
                token = knowledge_scope.set(("legacy",))
                try:
                    try:
                        candidates, _ = await paired.candidates(query["question"])
                        row["retrieval_seconds"] = candidates["retrieval_seconds"]
                        row["candidate_chunk_ids"] = [s["id"] for s in candidates["sources"]]
                        rrf = unique_documents(candidates["sources"], reverse)
                        row["rrf"] = {
                            "ranking": rrf,
                            "retrieval_metrics": retrieval_metrics(query["relevant"], rrf),
                        }
                        reranked = await paired.ranked(query["question"])
                        ranking = unique_documents(reranked["ranked"], reverse)
                        row["rerank_seconds"] = reranked["rerank_seconds"]
                        row["rerank"] = {
                            "ranking": ranking,
                            "retrieval_metrics": retrieval_metrics(query["relevant"], ranking),
                        }
                    except Exception as exc:
                        for variant in ("rrf", "rerank"):
                            row.setdefault(
                                variant,
                                {
                                    "ranking": [],
                                    "retrieval_metrics": retrieval_metrics(query["relevant"], []),
                                    "retrieval_error": exc.code
                                    if isinstance(exc, AppError)
                                    else type(exc).__name__,
                                },
                            )
                    if args.dataset == "crud":
                        # Counterbalance order, to avoid always giving the reranked answer a later time slot.
                        for variant in ("rrf", "rerank") if index % 2 == 0 else ("rerank", "rrf"):
                            started = time.perf_counter()
                            with collect_timings() as stage_timings:
                                try:
                                    conversation = await asyncio.to_thread(
                                        services.repository.create_conversation,
                                        admin["id"],
                                        "evaluation " + query["id"],
                                        ["legacy"],
                                    )
                                    answer = await chats[variant].answer(
                                        conversation["id"], admin["id"], query["question"], "adaptive"
                                    )
                                    status = answer["status"]
                                    evaluated = answer["answer"] if status in {"answered", "demo"} else ""
                                    citations = unique_documents(answer["sources"], reverse)
                                    row[variant]["answer"] = {
                                        "status": status,
                                        "text": answer["answer"],
                                        "metrics": answer_metrics(evaluated, query["answers"]),
                                        "citations": citations,
                                        "citation_source_recall": (
                                            len(set(citations) & query["relevant"].keys())
                                            / len(query["relevant"])
                                        )
                                        if query["relevant"]
                                        else None,
                                        "sources": answer["sources"],
                                        "trace": answer["trace"],
                                        "seconds": time.perf_counter() - started,
                                    }
                                except Exception as exc:
                                    row[variant]["answer"] = {
                                        "status": "error",
                                        "error": exc.code
                                        if isinstance(exc, AppError)
                                        else type(exc).__name__,
                                        "text": "",
                                        "metrics": answer_metrics("", query["answers"]),
                                        "seconds": time.perf_counter() - started,
                                    }
                            row[variant]["answer"]["stage_timings"] = stage_timings
                finally:
                    knowledge_scope.reset(token)
                row["execution"]["completed_at"] = datetime.now(UTC).isoformat()
                return row

            return process

        dependency_guard = DependencyFailureGuard()

        def commit_query(query, row):
            checkpoint.put("results", query["id"], row)
            results[query["id"]] = row
            dependency_guard.observe(row)
            progress(
                directory,
                "query",
                done=len(results),
                total=len(dataset.queries),
                id=query["id"],
                question_concurrency=args.query_concurrency,
                rrf_recall4=row["rrf"]["retrieval_metrics"].get("recall@4"),
                rerank_recall4=row["rerank"]["retrieval_metrics"].get("recall@4"),
                answers={v: row[v].get("answer", {}).get("status") for v in ("rrf", "rerank")},
            )

        history = read_json(directory / "execution-history.json", [])
        history.append(
            {
                "started_at": datetime.now(UTC).isoformat(),
                "completed_before_start": len(results),
                "question_concurrency": args.query_concurrency,
                "llm_max_concurrency": settings.llm_max_concurrency,
                "rerank_max_concurrency": settings.rerank_max_concurrency,
                "moderation_max_concurrency": settings.moderation_max_concurrency,
            }
        )
        write_json(directory / "execution-history.json", history)
        progress(
            directory,
            "query",
            done=len(results),
            total=len(dataset.queries),
            question_concurrency=args.query_concurrency,
        )

        def before_question():
            check_capacity(directory, runtime)
            dependency_guard.check()

        await run_question_workers(
            dataset.queries,
            results,
            args.query_concurrency,
            make_worker,
            before_question,
            commit_query,
        )
        write_json(directory / "results.json", results)
        metrics = make_report(directory, dataset, results, "complete")
        progress(directory, "complete", done=len(results), total=len(dataset.queries))
        print(
            json.dumps(
                {"stage": "complete", "report": str(directory / "report.md"), "metrics": metrics},
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        checkpoint.close()
        if args.full and settings.vector_backend == "milvus" and services.store.client:
            # Leave persisted evaluation indexes intact, release only their loaded memory.
            try:
                await asyncio.to_thread(services.store.client.release_collection, services.store.collection)
            except Exception:
                pass
        await services.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, choices=["t2", "crud"])
    parser.add_argument(
        "--crud-official-directory",
        action="store_true",
        help="Include all 48 official corpus files; requires --dataset crud --full and a new run/index",
    )
    parser.add_argument(
        "--full", action="store_true", help="Use the complete corpus and all retrieval/QA questions"
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="Separate disk for encrypted evaluation SQL/keyring; reports stay in the project",
    )
    parser.add_argument("--questions", type=int, default=20)
    parser.add_argument("--documents", type=int, default=300)
    parser.add_argument(
        "--exact-documents", type=int, help="Reject any corpus size that differs from this explicit budget"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--query-concurrency",
        type=int,
        choices=range(1, 5),
        default=1,
        help="Concurrent question pairs; provider gates remain shared and unchanged",
    )
    parser.add_argument("--candidates", type=int, help="Optional assertion; must equal the shared profile")
    parser.add_argument("--config", type=Path, default=ROOT / ".env.eval")
    parser.add_argument(
        "--backend", choices=["local", "milvus"], help="Defaults to the evaluation configuration"
    )
    reuse = parser.add_mutually_exclusive_group()
    reuse.add_argument(
        "--reuse-vectors-from", help="Reuse matching corpus/chunks/embeddings from a previous run name"
    )
    reuse.add_argument(
        "--reuse-matching-vectors-from",
        help="Reuse only completed identical documents from an interrupted run; import all others normally",
    )
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Only regenerate reports from saved results; never call models",
    )
    args = parser.parse_args()
    if args.crud_official_directory and (args.dataset != "crud" or not args.full):
        parser.error("--crud-official-directory requires --dataset crud --full")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.run_name)
        or (args.reuse_vectors_from and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.reuse_vectors_from))
        or (
            args.reuse_matching_vectors_from
            and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.reuse_matching_vectors_from)
        )
        or not 1 <= args.questions <= 1000
        or not 1 <= args.documents <= 200000
        or (args.candidates is not None and not 1 <= args.candidates <= 100)
    ):
        parser.error("Invalid run name, question/corpus count or candidate count")
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        with RunLock(ROOT / "test-results/evaluation" / args.run_name):
            asyncio.run(evaluate(args))
    except Exception as exc:
        # Credentials or upstream text can occur in exception strings; show only safe identifiers.
        if safe_error(exc) != "evaluation_already_running":
            progress(
                ROOT / "test-results/evaluation" / args.run_name,
                "stopped" if safe_error(exc) == "evaluation_stop_requested" else "failed",
                error=safe_error(exc),
                frames=safe_error_frames(exc),
            )
        else:
            print(json.dumps({"stage": "failed", "error": safe_error(exc)}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
