"""Offline source-coverage and failure-path audit; never treat anchor matches as qrels."""

import argparse
import gzip
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from statistics import mean, median

from app.config import ROOT


def normalize_source(text):
    text = unicodedata.normalize("NFKC", text.replace("\\n", "\n").replace("\\r", "\r"))
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def source_anchors(text, length=48):
    # Ignore crawler metadata; anchors refer to news content, never QA answers.
    body = re.split(r"正文[：:]", text, maxsplit=1)[-1]
    body = normalize_source(body)
    if len(body) < length:
        return [body] if body else []
    starts = sorted({int((len(body) - length) * fraction) for fraction in (0, 0.25, 0.5, 0.75, 1)})
    return list(dict.fromkeys(body[start : start + length] for start in starts))


def make_matcher(references):
    import ahocorasick

    patterns = {}
    for key, reference in references.items():
        for index, anchor in enumerate(reference["anchors"]):
            patterns.setdefault(anchor, []).append((key, index))
    matcher = ahocorasick.Automaton()
    for pattern, values in patterns.items():
        matcher.add_word(pattern, values)
    matcher.make_automaton()
    return matcher


def matching_anchors(matcher, text):
    hits = {}
    for _, values in matcher.iter(normalize_source(text)):
        for key, index in values:
            hits.setdefault(key, set()).add(index)
    return hits


def audit(directory, output):
    data = json.loads((directory / "dataset.json").read_text(encoding="utf-8"))
    results = {
        r["id"]: r
        for r in (
            json.loads(line)
            for line in (directory / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    references, question_references = {}, {}
    for query in data["queries"]:
        keys = []
        for text in query["reference_sources"]:
            key = hashlib.sha256(text.encode()).hexdigest()
            references.setdefault(
                key, {"anchors": source_anchors(text), "corpus_hits": set(), "additional_hits": set()}
            )
            keys.append(key)
        question_references[query["id"]] = keys
    matcher = make_matcher(references)
    for text in data["corpus"].values():
        for key, indices in matching_anchors(matcher, text).items():
            references[key]["corpus_hits"].update(indices)
    print(json.dumps({"stage": "corpus_scanned", "references": len(references)}), flush=True)
    extra_files = sorted(
        (ROOT / ".cache/evaluation/datasets/crud/corpus-audit").glob("documents_hallu.txt_part_*")
    )
    for path in extra_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            for key, indices in matching_anchors(matcher, line).items():
                references[key]["additional_hits"].update(indices)
    detail = []
    for index, query in enumerate(data["queries"]):
        row = results[query["id"]]
        qkeys = question_references[query["id"]]
        cache_key = hashlib.sha256(json.dumps([query["question"], ["legacy"], 32]).encode()).hexdigest()
        cache_path = directory / "candidate-cache" / (cache_key + ".json.gz")
        if cache_path.exists():
            cache = json.loads(gzip.decompress(cache_path.read_bytes()))
        else:
            old = cache_path.with_suffix("")
            cache = json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
        source_matches = {}
        for label, sources in (
            ("candidates32", cache.get("sources", [])),
            ("rerank4", cache.get("ranked", [])[:4]),
            ("rerank8", cache.get("ranked", [])[:8]),
        ):
            hits = {}
            for source in sources:
                for key, indices in matching_anchors(matcher, source["text"]).items():
                    if key in qkeys:
                        hits.setdefault(key, set()).update(indices)
            source_matches[label] = {key: len(hits.get(key, set())) for key in qkeys}
        a = row["rerank"]["answer"]
        trace = a.get("trace", [])
        detail.append(
            {
                "id": query["id"],
                "task": query["task"],
                "status": a["status"],
                "f1": a["metrics"]["character_f1"],
                "rougeL": a["metrics"]["character_rougeL"],
                "generated": any(t["node"] == "generate" for t in trace),
                "first_relevant_sources": next(
                    (t["sources"] for t in trace if t["node"] == "grade_documents"), None
                ),
                "answer_characters": len(normalize_source(a["text"])),
                "reference_characters": len(normalize_source(query["answers"][0])),
                "source_coverage": [
                    {
                        "key": key,
                        "anchors": len(references[key]["anchors"]),
                        "corpus_matches": len(references[key]["corpus_hits"]),
                        "excluded_partition_matches": len(references[key]["additional_hits"]),
                        **{label: hits[key] for label, hits in source_matches.items()},
                    }
                    for key in qkeys
                ],
            }
        )
        if (index + 1) % 400 == 0:
            print(json.dumps({"stage": "candidates_audited", "questions": index + 1}), flush=True)
    by_task = {}
    for task in sorted({q["task"] for q in detail}):
        selected = [q for q in detail if q["task"] == task]
        answered = [q for q in selected if q["status"] == "answered"]
        insufficient = [q for q in selected if q["status"] == "insufficient"]

        def covered(q):
            return all(s["corpus_matches"] >= min(3, s["anchors"]) for s in q["source_coverage"])

        by_task[task] = {
            "questions": len(selected),
            "status": dict(Counter(q["status"] for q in selected)),
            "insufficient_before_generation": sum(not q["generated"] for q in insufficient),
            "insufficient_after_generation": sum(q["generated"] for q in insufficient),
            "no_source_anchor_found": sum(
                all(s["corpus_matches"] == 0 for s in q["source_coverage"]) for q in selected
            ),
            "all_sources_at_least_3_anchors": sum(covered(q) for q in selected),
            "insufficient_and_no_source_anchor": sum(
                all(s["corpus_matches"] == 0 for s in q["source_coverage"]) for q in insufficient
            ),
            "answered_character_f1": mean(q["f1"] for q in answered),
            "answered_character_rougeL": mean(q["rougeL"] for q in answered),
            "answered_median_length": median(q["answer_characters"] for q in answered),
            "reference_median_length": median(q["reference_characters"] for q in answered),
            "initial_source_anchor_in_top32_but_not_top4": sum(
                any(s["candidates32"] >= 1 for s in q["source_coverage"])
                and not any(s["rerank4"] >= 1 for s in q["source_coverage"])
                for q in selected
            ),
            "source_anchor_added_by_top8": sum(
                not any(s["rerank4"] >= 1 for s in q["source_coverage"])
                and any(s["rerank8"] >= 1 for s in q["source_coverage"])
                for q in selected
            ),
        }
    summary = {
        "scope": "offline diagnostic; exact normalized reference-news anchors, not human qrels or semantic answerability",
        "source_run": directory.name,
        "questions": len(detail),
        "references": len(references),
        "anchor_chars": 48,
        "anchors_per_reference_max": 5,
        "references_no_anchor_in_corpus": sum(not r["corpus_hits"] for r in references.values()),
        "references_new_anchor_in_excluded_partitions": sum(
            not r["corpus_hits"] and bool(r["additional_hits"]) for r in references.values()
        ),
        "excluded_partition_files_checked": len(extra_files),
        "by_task": by_task,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "questions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in detail), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / "test-results/evaluation/crud-full-production")
    parser.add_argument("--output", type=Path, default=ROOT / "test-results/evaluation/quality-audit")
    args = parser.parse_args()
    audit(args.run, args.output)


if __name__ == "__main__":
    main()
