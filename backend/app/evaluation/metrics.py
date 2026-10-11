"""Explicit retrieval and answer overlap metrics; no LLM self-score is an accuracy label."""

import math
import random
import re
import unicodedata
from collections import Counter
from statistics import mean


def unique_documents(sources, document_map):
    result = []
    for source in sources:
        identifier = document_map[source["metadata"]["document_id"]]
        if identifier not in result:
            result.append(identifier)
    return result


def retrieval_metrics(relevant, ranking):
    # Missing judgments are not measured failures or zero recall.
    if not relevant:
        return {}
    import ir_measures as ir

    ranking = list(dict.fromkeys(ranking))
    qrels = [ir.Qrel("q", str(key), int(value)) for key, value in relevant.items()]
    run = [ir.ScoredDoc("q", str(key), float(len(ranking) - index)) for index, key in enumerate(ranking)]
    measures = [metric @ k for k in (1, 4, 5, 10, 32) for metric in (ir.R, ir.P)] + [ir.nDCG @ 10]
    values = ir.calc_aggregate(measures, qrels, run) if run else dict.fromkeys(measures, 0.0)
    output = {}
    for k in (1, 4, 5, 10, 32):
        output[f"recall@{k}"] = values[ir.R @ k]
        output[f"precision@{k}"] = values[ir.P @ k]
        output[f"hit@{k}"] = float(bool(set(ranking[:k]) & relevant.keys()))
    output["ndcg@10"] = values[ir.nDCG @ 10]
    # ir_measures RR backend support differs for cutoffs: explicitly truncate the run.
    output["mrr@10"] = ir.calc_aggregate([ir.RR], qrels, run[:10])[ir.RR] if run else 0.0
    return output


def normalize_answer(text):
    text = re.sub(r"\[\d+\]", "", text)
    return "".join(
        c
        for c in unicodedata.normalize("NFKC", text).lower()
        if not c.isspace() and not unicodedata.category(c).startswith("P")
    )


class CharacterTokenizer:
    def tokenize(self, text):
        return list(normalize_answer(text))


def answer_metrics(prediction, references):
    from rouge_score.rouge_scorer import RougeScorer

    scorer = RougeScorer(["rougeL"], tokenizer=CharacterTokenizer())
    predicted = normalize_answer(prediction)
    best = {"normalized_em": 0.0, "character_f1": 0.0, "character_rougeL": 0.0}
    for reference in references:
        target = normalize_answer(reference)
        if not target:
            raise ValueError("Reference answer must not be empty")
        overlap = sum((Counter(predicted) & Counter(target)).values())
        f1 = 2 * overlap / (len(predicted) + len(target)) if predicted else 0.0
        best["normalized_em"] = max(best["normalized_em"], float(predicted == target))
        best["character_f1"] = max(best["character_f1"], f1)
        best["character_rougeL"] = max(
            best["character_rougeL"], scorer.score(reference, prediction)["rougeL"].fmeasure
        )
    return best


def aggregate(rows):
    if not rows:
        return {}
    return {key: mean(row[key] for row in rows) for key in rows[0]}


def answer_populations(answers):
    """Report selective answer quality with explicit coverage, never silently change the baseline."""
    answered = [a for a in answers if a["status"] == "answered"]
    excluded = Counter(a["status"] for a in answers if a["status"] != "answered")
    return {
        "answer_answered_only": aggregate([a["metrics"] for a in answered]),
        "answer_population": {
            "total": len(answers),
            "answered": len(answered),
            "excluded": len(answers) - len(answered),
            "excluded_by_status": dict(excluded),
            "answer_coverage": len(answered) / len(answers) if answers else None,
            "all_attempts_denominator": len(answers),
            "answered_only_denominator": len(answered),
        },
    }


def answer_population_table(variants):
    lines = [
        "| 正常回答题口径 | RRF | Qwen 精排 |",
        "|---|---:|---:|",
    ]
    for field, label in (
        ("answered", "正常回答题数 / 条件分母"),
        ("total", "已执行题数"),
        ("excluded", "排除题数"),
    ):
        values = [variants[v]["answer_population"][field] for v in ("rrf", "rerank")]
        lines.append(f"| {label} | {values[0]} | {values[1]} |")
    coverage = [variants[v]["answer_population"]["answer_coverage"] for v in ("rrf", "rerank")]
    shown = [f"{x:.2%}" if x is not None else "N/A" for x in coverage]
    lines.append(f"| 回答覆盖率 | {shown[0]} | {shown[1]} |")
    for key in ("normalized_em", "character_f1", "character_rougeL"):
        values = [variants[v]["answer_answered_only"].get(key) for v in ("rrf", "rerank")]
        shown = [f"{x:.2%}" if x is not None else "N/A（无正常回答）" for x in values]
        lines.append(f"| {key}（仅正常回答） | {shown[0]} | {shown[1]} |")
    lines += [
        "",
        "此口径仅计入 status=answered 的最终答案，排除资料不足、错误和安全拦截；生成后被否决属于资料不足，不重复扣除。正常回答不等于已人工确认正确。两组入选题集合可能不同，条件分数差不能单独归因于精排提升。全量端到端分数另列，覆盖率始终以全部已执行题为分母。",
    ]
    return lines


def latency(values):
    values = sorted(values)
    if not values:
        return {}
    return {
        "count": len(values),
        "mean_seconds": mean(values),
        **{
            f"p{int(p * 100)}_seconds": values[max(0, math.ceil(p * len(values)) - 1)]
            for p in (0.5, 0.95, 0.99)
        },
    }


def paired_interval(rows, key, seed=42, repeats=2000):
    deltas = [
        row["rerank"][key] - row["rrf"][key] for row in rows if key in row["rrf"] and key in row["rerank"]
    ]
    if not deltas:
        return {}
    rng = random.Random(seed)
    samples = sorted(mean(rng.choices(deltas, k=len(deltas))) for _ in range(repeats))
    return {
        "difference": mean(deltas),
        "bootstrap_95_low": samples[int(repeats * 0.025)],
        "bootstrap_95_high": samples[int(repeats * 0.975)],
        "pairs": len(deltas),
    }
