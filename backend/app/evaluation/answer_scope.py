"""Add answered-only metrics to a completed run using saved rows; no model or index access."""

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

from app.evaluation.metrics import aggregate, answer_population_table, answer_populations
from app.evaluation.run import read_json, write_json

START = "<!-- ANSWER_POPULATION:START -->"
END = "<!-- ANSWER_POPULATION:END -->"


def refresh(directory):
    source = (directory / "queries.jsonl").read_bytes()
    rows = [json.loads(line) for line in source.decode("utf-8").splitlines() if line]
    metrics = read_json(directory / "metrics.json", {})
    if (
        metrics.get("status") != "complete"
        or len(rows) != metrics.get("completed_queries")
        or len(rows) != metrics.get("planned_queries")
        or len({r["id"] for r in rows}) != len(rows)
    ):
        raise ValueError("Completed, unique source rows are required")
    additions = {}
    for variant in ("rrf", "rerank"):
        answers = [row[variant]["answer"] for row in rows]
        original = metrics["variants"][variant]["answer"]
        recomputed = aggregate([a["metrics"] for a in answers])
        if original.keys() != recomputed.keys() or any(
            not math.isclose(original[key], value, rel_tol=1e-12, abs_tol=1e-12)
            for key, value in recomputed.items()
        ):
            raise ValueError("Saved all-attempt metrics disagree with original rows")
        additions[variant] = answer_populations(answers)
    # Validate both groups before writing; existing baseline values and raw rows are preserved.
    for variant, value in additions.items():
        metrics["variants"][variant].update(value)
    for task, section in metrics.get("by_task", {}).items():
        for variant in ("rrf", "rerank"):
            section[variant].update(
                answer_populations([r[variant]["answer"] for r in rows if r.get("task") == task])
            )
    evidence = {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_rows": len(rows),
        "filter": "final answer status == answered; never use rejected candidates",
        "note": "Conditional text overlap, not end-to-end or semantic accuracy; existing baseline unchanged",
        "variants": additions,
    }
    write_json(directory / "answer-scopes.json", evidence)
    write_json(directory / "metrics.json", metrics)
    path = directory / "report.md"
    original_report = path.read_text(encoding="utf-8")
    section = "\n".join(
        [START, "", "## 正常回答题质量与覆盖率", "", *answer_population_table(additions), "", END]
    )
    if START in original_report:
        before, rest = original_report.split(START, 1)
        _, after = rest.split(END, 1)
        revised = before + section + after
    else:
        revised = original_report.rstrip() + "\n\n" + section + "\n"
    path.write_text(revised, encoding="utf-8")
    csv_path = directory / "metrics.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8", newline="") as handle:
            existing = list(csv.reader(handle))
        existing = [
            row
            for row in existing
            if len(row) < 2 or row[1] not in {"answer_answered_only", "answer_population"}
        ]
        for variant, value in additions.items():
            for category, values in value.items():
                existing.extend(
                    [variant, category, key, metric]
                    for key, metric in values.items()
                    if not isinstance(metric, dict)
                )
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(existing)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(refresh(args.run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
