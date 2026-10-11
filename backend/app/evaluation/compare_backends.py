"""Compare completed local and Milvus runs offline; never calls model APIs."""

import argparse
import json
import re
from statistics import mean

from app.config import ROOT


def read(directory, name):
    return json.loads((directory / name).read_text(encoding="utf-8"))


def compare(local, milvus):
    old, new = read(local, "dataset.json"), read(milvus, "dataset.json")
    if old != new:
        raise ValueError("Backend comparison requires identical questions and corpus")
    a, b = read(local, "metrics.json"), read(milvus, "metrics.json")
    if a["status"] != "complete" or b["status"] != "complete":
        raise ValueError("Both runs must be complete")
    if b.get("vector_backend") != "milvus":
        raise ValueError("The target run is not a Milvus experiment")
    reuse = read(milvus, "vector-reuse.json")
    if reuse["source_run"] != local.name:
        raise ValueError("Target did not reuse this baseline's document vectors")
    rows_a, rows_b = read(local, "results.json"), read(milvus, "results.json")
    compared = {
        "local_run": local.name,
        "milvus_run": milvus.name,
        "dataset": b["dataset"],
        "questions": len(old["queries"]),
        "documents": len(old["corpus"]),
        "same_document_vectors": True,
        "milvus": read(milvus, "milvus-index.json"),
        "variants": {},
    }
    for variant in ("rrf", "rerank"):
        exact = sum(
            rows_a[key][variant]["ranking"][:10] == rows_b[key][variant]["ranking"][:10] for key in rows_a
        )
        overlaps = [
            len(set(rows_a[key][variant]["ranking"][:10]) & set(rows_b[key][variant]["ranking"][:10]))
            / max(1, len(set(rows_a[key][variant]["ranking"][:10])))
            for key in rows_a
        ]
        compared["variants"][variant] = {
            "local": a["variants"][variant],
            "milvus": b["variants"][variant],
            "same_top10_order_queries": exact,
            "mean_top10_document_overlap": mean(overlaps),
            "metric_delta": {
                key: b["variants"][variant]["retrieval"][key] - value
                for key, value in a["variants"][variant]["retrieval"].items()
            },
        }
    compared["timing_note"] = (
        "Retrieval measurements include query embedding API. Chat measurements use cached retrieval. No pure vector-store latency or QPS claim."
    )
    compared["quality_note"] = (
        "Small corpus subsets; fresh query embeddings and rerank/LLM calls. Generation differences cannot be attributed solely to vector backend."
    )
    (milvus / "backend-comparison.json").write_text(
        json.dumps(compared, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# {b['dataset']}：本地与 Milvus 对照",
        "",
        f"固定 {len(old['queries'])} 题、{len(old['corpus'])} 篇；复用完全相同的文档分块与向量。",
        "",
        "| 指标 | local RRF | Milvus RRF | local + Qwen | Milvus + Qwen |",
        "|---|---:|---:|---:|---:|",
    ]
    for key in ("recall@4", "recall@10", "ndcg@10", "mrr@10"):
        values = [m["variants"][v]["retrieval"][key] for v in ("rrf", "rerank") for m in (a, b)]
        lines.append(f"| {key} | " + " | ".join(f"{v:.4f}" for v in values) + " |")
    if "answer" in a["variants"]["rrf"]:
        lines += [
            "",
            "回答指标仅表示文本重合，不是语义准确率。",
            "",
            "| 指标 | local RRF | Milvus RRF | local + Qwen | Milvus + Qwen |",
            "|---|---:|---:|---:|---:|",
        ]
        for key in ("character_f1", "character_rougeL"):
            values = [m["variants"][v]["answer"][key] for v in ("rrf", "rerank") for m in (a, b)]
            lines.append(f"| {key} | " + " | ".join(f"{v:.4f}" for v in values) + " |")
    lines += [
        "",
        "## 比较边界",
        "",
        "- 相同文档向量，不复制旧候选排名或答案；问题向量与模型调用重新执行。",
        "- 小语料、随机干扰资料，对官方全集/真实业务规模不具备直接代表性。",
        "- 两套 BM25 实现、相似度过滤和 ANN 搜索不同，指标相同不代表结果顺序完全相同。",
        "- 查询时间包含在线 Embedding；问答时间复用候选缓存，不能据此判断向量库吞吐或冷请求速度。",
        "- 新一轮生成存在模型随机性及资料排序变化，回答差值不能全部归因于 Milvus。",
        "",
    ]
    (milvus / "backend-comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return compared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", required=True)
    parser.add_argument("--milvus", required=True)
    args = parser.parse_args()
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) for value in (args.local, args.milvus)):
        parser.error("Invalid run name")
    root = ROOT / "test-results/evaluation"
    result = compare(root / args.local, root / args.milvus)
    print(
        json.dumps(
            {
                "status": "compared",
                "questions": result["questions"],
                "report": str(root / args.milvus / "backend-comparison.md"),
            }
        )
    )


if __name__ == "__main__":
    main()
