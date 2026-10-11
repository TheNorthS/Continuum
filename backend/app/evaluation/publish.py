"""Publish completed evaluations as a compact README summary and a detailed report."""

import argparse
import json
from pathlib import Path

from app.config import ROOT
from app.evaluation.metrics import answer_population_table
from app.evaluation.plan import DEFAULT_PLAN, load_plan

START = "<!-- FULL_EVALUATION:START -->"
END = "<!-- FULL_EVALUATION:END -->"
RUNS = {"t2": "t2-full-production", "crud": "crud-full-production"}
EXPECTED = {"t2": (22812, 118605), "crud": (2394, 86835)}


def validated_results(root=ROOT, plan=None):
    output = {}
    fingerprints = set()
    implementations = set()
    scopes = (
        plan["runs"]
        if plan
        else {
            kind: {
                "run_name": name,
                "questions": EXPECTED[kind][0],
                "documents": EXPECTED[kind][1],
                "full": True,
            }
            for kind, name in RUNS.items()
        }
    )
    for kind, scope in scopes.items():
        name = scope["run_name"]
        directory = root / "test-results/evaluation" / name
        metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        verified = json.loads((directory / "milvus-index.json").read_text(encoding="utf-8"))
        implementation = json.loads((directory / "implementation.json").read_text(encoding="utf-8"))
        queries, documents = scope["questions"], scope["documents"]
        if not (
            metrics["status"] == "complete"
            and bool(metrics.get("full_dataset")) == scope["full"]
            and metrics["completed_queries"] == metrics["planned_queries"] == queries
            and metrics["corpus_size"] == index["documents"] == documents
            and index["chunks"] == verified["chunks_verified"] > 0
            and bool(manifest["contract"]["dataset"].get("full")) == scope["full"]
            and metrics["vector_backend"] == "milvus"
        ):
            raise ValueError("Planned evaluation is incomplete or its scope changed: " + name)
        fingerprints.add(metrics["pipeline_fingerprint"])
        implementations.add(implementation["fingerprint"])
        output[kind] = metrics
    if len(fingerprints) != 1 or None in fingerprints:
        raise ValueError("Evaluations use different pipeline configurations")
    if len(implementations) != 1 or None in implementations:
        raise ValueError("Evaluations use different RAG implementations")
    return output


def charts(results, directory):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    directory.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )
    colors = ("#64748b", "#2563eb")
    variants = ("rrf", "rerank")
    labels = ("RRF baseline", "RRF + Qwen")

    def save(fig, name):
        fig.savefig(directory / (name + ".png"), dpi=180, bbox_inches="tight")
        fig.savefig(directory / (name + ".svg"), bbox_inches="tight")
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4), layout="constrained")
    keys = ("recall@4", "mrr@10", "ndcg@10", "precision@4")
    x = np.arange(len(keys))
    for n, variant in enumerate(variants):
        values = [results["t2"]["variants"][variant]["retrieval"][key] for key in keys]
        bars = axes[0].bar(x + (n - 0.5) * 0.36, values, 0.36, label=labels[n], color=colors[n])
        axes[0].bar_label(bars, fmt="%.3f", fontsize=8, padding=3)
        cutoffs = (1, 4, 5, 10, 32)
        axes[1].plot(
            cutoffs,
            [results["t2"]["variants"][variant]["retrieval"][f"recall@{k}"] for k in cutoffs],
            marker="o",
            label=labels[n],
            color=colors[n],
        )
    axes[0].set_xticks(x, ["Recall@4", "MRR@10", "nDCG@10", "Precision@4"])
    axes[0].set_title(f"T2Retrieval | {results['t2']['completed_queries']:,} questions")
    axes[1].set_title("Recall across document cutoffs")
    axes[1].set_xlabel("K (unique source documents)")
    for ax in axes:
        ax.set_ylim(0, 1.12)
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(axis="y", alpha=0.15)
        ax.set_axisbelow(True)
    save(fig, "retrieval")

    conditional = all("answer_population" in results["crud"]["variants"][v] for v in variants)
    fig, axes = plt.subplots(
        1, 3 if conditional else 2, figsize=(16 if conditional else 12, 4.8), layout="constrained"
    )
    keys = ("normalized_em", "character_f1", "character_rougeL")
    x = np.arange(3)
    statuses = sorted(
        {status for variant in variants for status in results["crud"]["variants"][variant]["answer_status"]}
    )
    for n, variant in enumerate(variants):
        values = [results["crud"]["variants"][variant]["answer"][key] for key in keys]
        bars = axes[0].bar(x + (n - 0.5) * 0.36, values, 0.36, label=labels[n], color=colors[n])
        axes[0].bar_label(bars, fmt="%.3f", fontsize=9, padding=3)
        if conditional:
            row = results["crud"]["variants"][variant]
            population = row["answer_population"]
            count = population["answered"]
            coverage = population["answer_coverage"]
            label = (
                f"{labels[n]}: n={count:,}, {coverage:.1%}" if coverage is not None else f"{labels[n]}: n=0"
            )
            bars = axes[1].bar(
                x + (n - 0.5) * 0.36,
                [row["answer_answered_only"].get(key, float("nan")) for key in keys],
                0.36,
                label=label,
                color=colors[n],
            )
            axes[1].bar_label(bars, fmt="%.3f", fontsize=9, padding=3)
        bars = axes[-1].bar(
            np.arange(len(statuses)) + (n - 0.5) * 0.36,
            [results["crud"]["variants"][variant]["answer_status"].get(s, 0) for s in statuses],
            0.36,
            label=labels[n],
            color=colors[n],
        )
        axes[-1].bar_label(bars, fontsize=9, padding=3)
    axes[0].set_xticks(x, ["EM", "Char F1", "Char ROUGE-L"])
    axes[0].set_ylim(0, 1.12)
    axes[0].set_title(f"All attempts | n={results['crud']['completed_queries']:,} each")
    if conditional:
        axes[1].set_xticks(x, ["EM", "Char F1", "Char ROUGE-L"])
        axes[1].set_ylim(0, 1.12)
        axes[1].set_title("Answered only | conditional quality")
    axes[-1].set_xticks(np.arange(len(statuses)), statuses, rotation=15)
    axes[-1].set_title(f"Answer outcomes | {results['crud']['completed_queries']:,} questions")
    fig.suptitle("CRUD QA: text overlap, not semantic accuracy | conditional populations differ", fontsize=12)
    for ax in axes:
        ax.legend(fontsize=9)
        ax.grid(axis="y", alpha=0.15)
        ax.set_axisbelow(True)
    save(fig, "answers")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4), layout="constrained")
    for ax, kind in zip(axes, ("t2", "crud"), strict=True):
        series = {
            "retrieval": results[kind]["initial_retrieval_latency"],
            "rerank": results[kind]["rerank_incremental_latency"],
        }
        if kind == "crud":
            groups = results[kind].get("by_question_concurrency", {})
            if groups:
                for concurrency, group in groups.items():
                    series.update(
                        {
                            f"{labels[n]} (Q={concurrency})": group["variants"][v]["chat_latency"]
                            for n, v in enumerate(variants)
                        }
                    )
            else:
                series.update(
                    {labels[n]: results[kind]["variants"][v]["chat_latency"] for n, v in enumerate(variants)}
                )
        for name, value in series.items():
            ax.plot(
                [50, 95, 99],
                [value.get(f"p{k}_seconds", float("nan")) for k in (50, 95, 99)],
                marker="o",
                label=name,
            )
        ax.set_title(kind.upper() + " | observed latency")
        ax.set_xlabel("Percentile")
        ax.set_ylabel("Seconds")
        ax.set_xticks([50, 95, 99])
        ax.grid(alpha=0.15)
        ax.legend(fontsize=9)
    fig.suptitle("Includes cache effects; not a production concurrency benchmark", fontsize=12)
    save(fig, "latency")

    fig, ax = plt.subplots(figsize=(9, 3.6), layout="constrained")
    keys = ("recall@4", "mrr@10", "ndcg@10")
    for n, key in enumerate(keys):
        interval = results["t2"]["paired_retrieval"][key]
        value = interval["difference"] * 100
        lower, upper = interval["bootstrap_95_low"] * 100, interval["bootstrap_95_high"] * 100
        ax.errorbar(
            value,
            n,
            xerr=[[max(0, value - lower)], [max(0, upper - value)]],
            fmt="o",
            capsize=5,
            color=colors[1],
        )
        ax.annotate(
            f" {value:+.2f} pp [{lower:+.2f}, {upper:+.2f}]",
            (value, n),
            xytext=(4, 10),
            textcoords="offset points",
            fontsize=9,
        )
    ax.axvline(0, color=colors[0], linestyle="--", linewidth=1)
    ax.set_yticks(range(3), ["Recall@4", "MRR@10", "nDCG@10"])
    ax.set_ylim(-0.6, 2.6)
    ax.set_xlabel("Qwen minus RRF (percentage points)")
    ax.set_title("Paired query bootstrap | 95% interval | 2,000 resamples")
    save(fig, "rerank-delta")


def readme_summary(t2):
    """Keep the project homepage focused on T2; complete results belong in docs."""
    scope = "文档（完整语料）" if t2["full_dataset"] else "文档子集"
    lines = [
        START,
        "",
        "## 公开数据集评测",
        "",
        f"以下为已完成的 **T2Retrieval 检索实验**：{t2['completed_queries']:,} 个问题、{t2['corpus_size']:,} 篇{scope}。两组使用相同初始候选池，RRF 为去精排对照，Qwen 组增加在线模型精排。",
        "",
        "| 检索指标 | RRF | RRF + Qwen |",
        "|---|---:|---:|",
    ]
    for label, key in (
        ("Recall@4", "recall@4"),
        ("Recall@10", "recall@10"),
        ("MRR@10", "mrr@10"),
        ("nDCG@10", "ndcg@10"),
    ):
        values = [t2["variants"][v]["retrieval"][key] for v in ("rrf", "rerank")]
        lines.append(f"| {label} | {values[0]:.4f} | **{values[1]:.4f}** |")
    delta = (
        t2["variants"]["rerank"]["retrieval"]["recall@4"] - t2["variants"]["rrf"]["retrieval"]["recall@4"]
    ) * 100
    change = f"提高 **{delta:.2f} 个百分点**" if delta >= 0 else f"下降 **{abs(delta):.2f} 个百分点**"
    lines += [
        "",
        f"本轮精排使 Recall@4 {change}。指标以去重来源文档为单位，使用官方相关性标注和 `ir_measures` 计算。",
        "",
        "![T2Retrieval：RRF 与 Qwen 精排的检索指标及 Recall@K](docs/assets/evaluation/retrieval.png)",
        "",
        "实验使用 Milvus、语义分块 1500/150、32 条候选，图谱融合关闭。检索报告的 K 表示去重来源文档数；生成配置的 Top-K 4 表示片段数。该实验只测检索，不生成答案；"
        + (
            "结果不代表官方榜单排名或生产并发性能。"
            if t2["full_dataset"]
            else "子集结果不代表完整数据集榜单成绩或生产并发性能。"
        ),
        "",
        "[实验方法](docs/EVALUATION_PLAN.md) · [运行与复现](docs/EVALUATION.md) · [完整实验记录](docs/FULL_EVALUATION.md)",
        "",
        END,
    ]
    return "\n".join(lines)


def publish(root=ROOT, plan=None):
    results = validated_results(root, plan)
    charts(results, root / "docs/assets/evaluation")
    lines = [
        START,
        "",
        "## 公开数据集评测",
        "",
        "两组使用同一生产 RAG 配置：Milvus、语义分块 1500/150、32 条候选、Top-K 4，图谱融合关闭。RRF 为去精排对照，Qwen 为正式链路。",
        "",
        "| 数据集 | 实际范围 | 指标 | RRF | RRF + Qwen |",
        "|---|---|---|---:|---:|",
    ]
    for kind, category, keys in (
        ("t2", "retrieval", ("recall@4", "recall@10", "mrr@10", "ndcg@10")),
        ("crud", "answer", ("normalized_em", "character_f1", "character_rougeL")),
    ):
        for key in keys:
            values = [results[kind]["variants"][v][category][key] for v in ("rrf", "rerank")]
            result = results[kind]
            scope_label = (
                "QA 全部问题，45/48 个语料分片"
                if kind == "crud" and result["corpus_size"] == 86835
                else "QA 全部问题，语料范围见实验说明"
                if kind == "crud"
                else "全量"
                if result["full_dataset"]
                else "子集"
            )
            scope = f"{result['completed_queries']:,} 问题 / {result['corpus_size']:,} 文档（{scope_label}）"
            label = "CRUD（全量端到端）" if kind == "crud" else kind.upper()
            lines.append(f"| {label} | {scope} | {key} | {values[0]:.4f} | {values[1]:.4f} |")
    lines += [
        "",
        "CRUD 字符指标衡量文本重合，不等于语义准确率；全库没有完整 qrels。上表保留全量端到端基线；下表仅评价正常返回的答案，资料不足、错误与拦截不进入条件指标分母。",
        "",
    ]
    if all("answer_population" in results["crud"]["variants"][v] for v in ("rrf", "rerank")):
        lines += [*answer_population_table(results["crud"]["variants"]), ""]
    for v in ("rrf", "rerank"):
        lines.append(
            f"- {v}：T2 检索错误 {results['t2']['variants'][v]['retrieval_errors']}；CRUD 检索错误 {results['crud']['variants'][v]['retrieval_errors']}；CRUD 回答状态 `{json.dumps(results['crud']['variants'][v]['answer_status'], ensure_ascii=False)}`。"
        )
    for name, caption in (
        ("retrieval", "T2 检索与 Recall@K"),
        ("rerank-delta", "精排配对变化及置信区间"),
        ("answers", "CRUD 回答指标与状态分布"),
        ("latency", "p50 / p95 / p99 耗时"),
    ):
        lines += ["", f"![{caption}](docs/assets/evaluation/{name}.png)"]
    lines += [
        "",
        "回答指标使用 rouge-score，检索指标使用 ir_measures；未使用 LLM 裁判/Ragas。T2 如标为子集，其候选语料较小、检索可能更容易，不能作为完整测试集成绩。两组均不是官方排行榜或生产并发压测。",
        "",
        "[完整结果与图表](docs/FULL_EVALUATION.md) · [复现说明](docs/EVALUATION.md) · [质量诊断](docs/QUALITY_DIAGNOSIS.md) · [聚合指标](docs/assets/evaluation/metrics.json)",
        "",
        END,
    ]
    full_section = "\n".join(lines).replace("## 公开数据集评测", "## 指标与图表", 1)
    # Public links must resolve in a clean clone; raw run artifacts stay local.
    full_section = full_section.replace("](docs/", "](")
    section = readme_summary(results["t2"])
    readme = root / "README.md"
    original = readme.read_text(encoding="utf-8")
    if START in original:
        before, rest = original.split(START, 1)
        _, after = rest.split(END, 1)
        revised = before + section + after
    else:
        raise ValueError("README insertion marker missing")
    report = root / "docs/FULL_EVALUATION.md"
    report_original = report.read_text(encoding="utf-8")
    if START not in report_original or END not in report_original:
        raise ValueError("Evaluation report insertion marker missing")
    report_before, report_rest = report_original.split(START, 1)
    _, report_after = report_rest.split(END, 1)
    report_revised = report_before + full_section + report_after
    # Re-read before commit: do not overwrite a concurrent user edit.
    if readme.read_text(encoding="utf-8") != original:
        raise RuntimeError("README changed while publishing")
    if report.read_text(encoding="utf-8") != report_original:
        raise RuntimeError("Evaluation report changed while publishing")
    temporary = readme.with_suffix(".md.tmp")
    temporary.write_text(revised, encoding="utf-8")
    temporary.replace(readme)
    temporary_report = report.with_suffix(".md.tmp")
    temporary_report.write_text(report_revised, encoding="utf-8")
    temporary_report.replace(report)
    (root / "docs/assets/evaluation/metrics.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    args = parser.parse_args()
    publish(args.root, load_plan(args.plan))
    print("Planned evaluation charts, README summary and detailed report updated locally.", flush=True)


if __name__ == "__main__":
    main()
