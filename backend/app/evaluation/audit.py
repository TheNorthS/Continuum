"""Compare effective RAG policy without connecting to databases or model providers."""

import argparse
import json
from pathlib import Path

from app.config import ROOT, Settings
from app.evaluation.configuration import load_settings
from app.pipeline_profile import effective_pipeline, pipeline_fingerprint


def audit(production, evaluation):
    # Audit algorithm settings only; host/domain/secret mounts are validated during
    # the real production startup. Never create an app or connect to a store here.
    prod = Settings(_env_file=production, environment="test")
    test = load_settings(evaluation)
    left, right = effective_pipeline(prod), effective_pipeline(test)
    differences = [key for key in left if left[key] != right[key]]
    return {
        "aligned": not differences,
        "production_profile": str(prod.pipeline_profile),
        "evaluation_profile": str(test.pipeline_profile),
        "production_fingerprint": pipeline_fingerprint(prod),
        "evaluation_fingerprint": pipeline_fingerprint(test),
        "differences": differences,
        "pipeline": left,
        "scope": "RAG policy parity; not production infrastructure/security readiness",
        "allowed_isolation": [
            "SQL database and keys",
            "Milvus collection and connection",
            "Neo4j namespace and connection",
            "Redis database",
            "domain, account and authentication secrets",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", type=Path, default=ROOT / ".env.production")
    parser.add_argument("--evaluation", type=Path, default=ROOT / ".env.eval")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.production, args.evaluation)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps({key: value for key, value in result.items() if key != "pipeline"}, ensure_ascii=False)
        )
        if not result["aligned"]:
            raise SystemExit(1)
    except Exception as exc:
        # Do not print Pydantic input dictionaries containing credentials.
        print(json.dumps({"aligned": False, "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
