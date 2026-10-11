"""Small diagnostic interventions on saved contexts, never a replacement benchmark."""

import argparse
import asyncio
import gzip
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from app.agents.orchestrator import Orchestrator
from app.config import ROOT
from app.evaluation.configuration import load_settings
from app.evaluation.metrics import answer_metrics
from app.evaluation.run import Usage, read_json, write_json
from app.infrastructure.container import Container


def load_cases(directory):
    data = read_json(directory / "dataset.json", {})
    queries = {q["id"]: q for q in data["queries"]}
    rows = {
        r["id"]: r
        for r in (
            json.loads(line)
            for line in (directory / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    audit = [
        json.loads(line)
        for line in (ROOT / "test-results/evaluation/quality-audit/questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    groups = {
        "no_reference_anchor": lambda a: (
            a["status"] == "insufficient"
            and a["task"] == "questanswer_1doc"
            and all(s["corpus_matches"] == 0 for s in a["source_coverage"])
        ),
        "reference_found_below_top4": lambda a: (
            a["status"] == "insufficient"
            and not any(s["rerank4"] for s in a["source_coverage"])
            and any(s["rerank8"] for s in a["source_coverage"])
        ),
        "reference_in_top4_but_rejected": lambda a: (
            a["status"] == "insufficient"
            and a["generated"]
            and all(s["rerank4"] >= min(3, s["anchors"]) for s in a["source_coverage"])
        ),
        "answered_low_overlap": lambda a: (
            a["status"] == "answered"
            and a["task"] != "questanswer_1doc"
            and a["f1"] < 0.4
            and a["answer_characters"] > a["reference_characters"] * 1.8
        ),
    }
    selected, used = [], set()
    for group, predicate in groups.items():
        available = sorted((a for a in audit if predicate(a) and a["id"] not in used), key=lambda a: a["id"])
        for item in available[:3]:
            q = queries[item["id"]]
            key = hashlib.sha256(json.dumps([q["question"], ["legacy"], 32]).encode()).hexdigest()
            path = directory / "candidate-cache" / (key + ".json.gz")
            cache = (
                json.loads(gzip.decompress(path.read_bytes()))
                if path.exists()
                else read_json(path.with_suffix(""), {})
            )
            if len(cache.get("ranked", [])) < 8:
                continue
            selected.append(
                {"group": group, "query": q, "baseline": rows[q["id"]], "ranked": cache["ranked"]}
            )
            used.add(q["id"])
    return selected


class CapturedKnowledge:
    def __init__(self, sources):
        self.sources = sources

    async def search(self, query, top_k):
        return self.sources[:top_k]


async def probe(args):
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    cases = load_cases(args.run)
    selection = {
        "created_at": datetime.now(UTC).isoformat(),
        "cases": [{"id": c["query"]["id"], "group": c["group"]} for c in cases],
        "purpose": "purposively selected failure cases; intervention, not unbiased quality estimate",
        "oracle_scope": "reference news supplied as context, no standard answers in model prompts; no production index writes",
        "limitations": "uses fixed initial contexts, no retrieval rewrites; rerun nondeterminism possible; top8 is not deployed",
    }
    if (output / "selection.json").exists():
        old = read_json(output / "selection.json", {})
        if old["cases"] != selection["cases"]:
            raise ValueError("Probe selection changed")
    else:
        write_json(output / "selection.json", selection)
    results = read_json(output / "results.json", {})
    settings = load_settings(workspace=output / ".runtime")
    services = Container(settings)
    usage = Usage(output / "usage.json")
    services.http.event_hooks["response"].append(usage.response)
    services.moderation.http.event_hooks["response"].append(usage.response)
    try:
        # Only isolated SQL is needed by audit logging. Do not create/load another
        # Milvus index or alter the saved retrieval corpus for this intervention.
        await asyncio.to_thread(services.db.migrate)
        await asyncio.to_thread(services._migrate_encryption)
        for case in cases:
            query = case["query"]
            for arm in ("saved_top4", "saved_top8", "reference_news"):
                key = query["id"] + "/" + arm
                if key in results:
                    continue
                sources = (
                    case["ranked"][: 4 if arm == "saved_top4" else 8]
                    if arm != "reference_news"
                    else [
                        {
                            "id": f"oracle-{i}",
                            "text": text,
                            "score": 1,
                            "metadata": {"source": f"reference-news-{i}"},
                        }
                        for i, text in enumerate(query["reference_sources"], 1)
                    ]
                )
                row = {
                    "id": query["id"],
                    "group": case["group"],
                    "arm": arm,
                    "question": query["question"],
                    "reference_answer": query["answers"][0],
                    "baseline_status": case["baseline"]["rerank"]["answer"]["status"],
                    "baseline_answer": case["baseline"]["rerank"]["answer"]["text"],
                    "baseline_metrics": case["baseline"]["rerank"]["answer"]["metrics"],
                }
                try:
                    async with asyncio.timeout(180):
                        await services.moderation.guard(query["question"], "input")
                        filtered = await services.moderation.filter_sources(sources)
                        grades = await services.llm.batch(
                            "relevance",
                            [{"question": query["question"], "document": s["text"]} for s in filtered],
                        )
                        relevant = [s for s, g in zip(filtered, grades, strict=True) if g["relevant"]]
                        row.update(
                            input_contexts=len(sources),
                            safe_contexts=len(filtered),
                            relevant_contexts=len(relevant),
                            sources=[{"id": s["id"], "text": s["text"]} for s in relevant],
                        )
                        if not relevant:
                            row.update(
                                status="no_relevant_evidence", metrics=answer_metrics("", query["answers"])
                            )
                        else:
                            flow = Orchestrator(
                                settings,
                                services.llm,
                                CapturedKnowledge(relevant),
                                services.web,
                                services.tracing,
                                services.moderation,
                            )
                            state = {
                                "original_question": query["question"],
                                "query": query["question"],
                                "sources": relevant,
                                "history": [],
                                "generations": 0,
                                "rewrites": 0,
                                "web_used": False,
                            }
                            generated = await flow.generate(state)
                            state.update(generated)
                            checked = await flow.grade_answer(state)
                            row.update(
                                answer=state["answer"],
                                supported=checked["supported"],
                                useful=checked["useful"],
                                status="checks_passed"
                                if checked["supported"] and checked["useful"]
                                else "checks_rejected",
                                metrics=answer_metrics(state["answer"], query["answers"]),
                            )
                except Exception as exc:
                    row.update(status="error", error=getattr(exc, "code", type(exc).__name__))
                results[key] = row
                write_json(output / "results.json", results)
                print(
                    json.dumps(
                        {
                            "done": len(results),
                            "planned": len(cases) * 3,
                            "id": query["id"],
                            "arm": arm,
                            "status": row["status"],
                        }
                    ),
                    flush=True,
                )
        summary = {}
        for group in sorted({c["group"] for c in cases}):
            summary[group] = {}
            for arm in ("saved_top4", "saved_top8", "reference_news"):
                rows = [r for r in results.values() if r["group"] == group and r["arm"] == arm]
                summary[group][arm] = {
                    "n": len(rows),
                    "statuses": dict(Counter(r["status"] for r in rows)),
                    "diagnostic_raw_answer_f1": mean(
                        r.get("metrics", {}).get("character_f1", 0) for r in rows
                    )
                    if rows
                    else None,
                }
        write_json(output / "summary.json", summary)
    finally:
        await services.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / "test-results/evaluation/crud-full-production")
    parser.add_argument("--output", type=Path, default=ROOT / "test-results/evaluation/root-cause-probe")
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
