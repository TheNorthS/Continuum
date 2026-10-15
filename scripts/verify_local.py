"""Run against local demo servers; writes evidence, never prints access tokens."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.security_client import SecurityClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--frontend", default="http://127.0.0.1:5173")
    parser.add_argument("--report", default="local-smoke.json")
    parser.add_argument("--legacy-root", type=Path, help="Optional original datas directory for migration audit")
    args = parser.parse_args()
    report = {}
    if args.legacy_root:
        originals = args.legacy_root.resolve(strict=True)
        mappings = [
            (originals / "md", ROOT / "data/documents/md"),
            (originals / "output", ROOT / "data/legacy/output"),
        ]
        for source, destination in mappings:
            files = list(source.glob("*"))
            assert files, "Legacy audit directory is empty"
            assert all(
                hashlib.sha256(p.read_bytes()).digest()
                == hashlib.sha256((destination / p.name).read_bytes()).digest()
                for p in files
                if p.is_file()
            )
            report[source.name + "_files_verified"] = len([p for p in files if p.is_file()])
        assert (originals / "layout-parser-paper.pdf").read_bytes() == (
            ROOT / "data/legacy/layout-parser-paper.pdf"
        ).read_bytes()
        report["legacy_pdf_verified"] = True
    else:
        report["legacy_audit"] = "not requested; public demo uses bundled synthetic documents"
    with SecurityClient(base_url=args.api, timeout=160) as client:
        client.login("admin", "rag-e-demo-2026")
        report["ready"] = client.get("/health/ready").json()
        report["stats"] = client.get("/admin/stats").json()
        hits = client.post(
            "/knowledge/search", json={"query": "Milvus hybrid search", "top_k": 4}
        )
        hits.raise_for_status()
        assert hits.json()["results"]
        report["search_sources"] = [
            h["metadata"]["filename"] for h in hits.json()["results"]
        ]
        for mode in ("adaptive", "agentic"):
            created = client.post("/conversations", json={"title": "本地验收 " + mode})
            created.raise_for_status()
            cid = created.json()["id"]
            try:
                response = client.post(
                    f"/conversations/{cid}/stream",
                    json={"question": "Milvus 如何工作？", "mode": mode},
                )
                response.raise_for_status()
                events = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                answer = events[-1]
                assert answer["sources"] and answer["runtime"] == "demo"
                assert len(client.get(f"/conversations/{cid}/messages").json()) == 2
                report[mode] = {
                    "status": answer["status"],
                    "sources": len(answer["sources"]),
                    "nodes": [s["node"] for s in answer["trace"]],
                }
            finally:
                client.delete(f"/conversations/{cid}").raise_for_status()
    with httpx.Client(base_url=args.frontend) as frontend:
        response = frontend.get("/")
        response.raise_for_status()
        assert "知恒 · Continuum" in response.text
        assert frontend.get("/api/v1/health/live").status_code == 200
        report["frontend_http_and_proxy"] = "ok"
    target = ROOT / "test-results" / Path(args.report).name
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
