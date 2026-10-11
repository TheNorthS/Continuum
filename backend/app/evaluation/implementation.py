"""Pin executable RAG code and relevant dependency versions across long runs."""

import hashlib
import importlib.metadata
import json
from pathlib import Path


def implementation_record():
    root = Path(__file__).parents[1]
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
        if "evaluation" not in path.relative_to(root).parts
    }
    packages = {
        name: importlib.metadata.version(name)
        for name in ("pymilvus", "sqlalchemy", "httpx", "cryptography", "langgraph", "numpy")
    }
    payload = {"files": files, "packages": packages}
    return {
        **payload,
        "fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
    }


def pin_implementation(directory):
    path = directory / "implementation.json"
    record = implementation_record()
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != record:
            raise ValueError("RAG implementation changed; use a new full evaluation run")
    else:
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
