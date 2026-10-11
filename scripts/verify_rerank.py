"""Run real local reranking on synthetic passages without changing business data."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.infrastructure.container import Container


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=str(ROOT / ".cache/models/bge-reranker-base"))
    args = parser.parse_args()
    settings = Settings(rerank_provider="huggingface", rerank_model=args.model, rerank_timeout=120, graph_enabled=False, redis_url=SecretStr(""))
    service = Container(settings)
    started = time.perf_counter()
    try:
        docs = [
            {"id": str(i), "text": text, "metadata": {}, "score": 0.1}
            for i, text in enumerate(["香蕉是一种水果。", "PostgreSQL 是关系数据库。", "EUV 光刻使用 13.5 纳米波长的极紫外光。"])
        ]
        print("Loading model and scoring Chinese examples...", flush=True)
        result = await service.reranker.rank("EUV 光刻波长是多少？", docs)
        assert result[0]["id"] == "2"
        report = {"model": service.reranker.model_name, "initial_seconds": round(time.perf_counter() - started, 3), "chinese_order": [r["id"] for r in result], "scores": [r["rerank_score"] for r in result]}
        print(json.dumps(report), flush=True)
        started = time.perf_counter()
        candidates = [
            {"id": f"benchmark-{i}", "text": ("数据库事务与备份维护。" if i < 31 else "EUV 光刻波长为13.5纳米。") * 50, "score": 0.1, "metadata": {}}
            for i in range(32)
        ]
        hits = await service.reranker.rank("EUV 光刻波长是多少？", candidates)
        assert len(hits) == 32 and hits[0]["id"] == "benchmark-31"
        report["candidate_pool_seconds"] = round(time.perf_counter() - started, 3)
        report["candidate_count"] = len(hits)
        assert len(await service.reranker.rank("EUV 光刻波长是多少？", [docs[2]])) == 1
        report["state"] = service.reranker.status()["state"]
        report["status"] = "passed"
        target = ROOT / "test-results/rerank-local.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
