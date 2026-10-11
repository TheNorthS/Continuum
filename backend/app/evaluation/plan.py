"""Explicit dataset scope, separate from the unchanged production RAG policy."""

import json
import re

from app.config import ROOT

DEFAULT_PLAN = ROOT / "config/evaluation.example.json"


def load_plan(path=DEFAULT_PLAN):
    value = json.loads(path.read_text(encoding="utf-8"))

    def valid_name(name):
        return isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name))

    if (
        value.get("version") != 1
        or not valid_name(value.get("suite_name"))
        or set(value.get("runs", {})) != {"t2", "crud"}
    ):
        raise ValueError("Invalid evaluation plan")
    for row in value["runs"].values():
        if (
            not valid_name(row.get("run_name"))
            or type(row.get("full")) is not bool
            or (
                row.get("reuse_matching_vectors_from") is not None
                and not valid_name(row["reuse_matching_vectors_from"])
            )
            or type(row.get("questions")) is not int
            or not 1 <= row["questions"] <= 100000
            or type(row.get("documents")) is not int
            or not 1 <= row["documents"] <= 200000
            or type(row.get("seed")) is not int
        ):
            raise ValueError("Invalid evaluation run scope")
    return value
