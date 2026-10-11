"""Signed HTTP acceptance against the local app; cleans up only its own conversation."""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.security_client import SecurityClient


def main():
    report = {}
    # Exercise the same frontend proxy path used by the browser.
    with SecurityClient("http://127.0.0.1:5173/api/v1", timeout=160) as client:
        client.login("admin", "rag-e-demo-2026")
        bases = client.get("/knowledge/bases").json()
        assert bases, "Local acceptance requires an existing knowledge base"
        ids = [bases[0]["id"]]
        documents = client.get("/knowledge/documents").json()
        allowed = {d["id"] for d in documents if d["base_id"] in ids}
        report["existing_base_count"] = len(bases)
        created = client.post("/conversations", json={"title": "选库问答临时验收", "base_ids": ids})
        created.raise_for_status()
        cid = created.json()["id"]
        try:
            assert client.get(f"/conversations/{cid}").json()["base_ids"] == ids
            search = client.post("/knowledge/search", json={"query": "Milvus", "top_k": 4, "base_ids": ids})
            search.raise_for_status()
            assert search.json()["results"]
            assert all(s["metadata"]["document_id"] in allowed for s in search.json()["results"])
            stream = client.post(f"/conversations/{cid}/stream", json={"question": "Milvus 如何工作？", "base_ids": ids})
            stream.raise_for_status()
            assert "event: result" in stream.text
            result = json.loads([line[6:] for line in stream.text.splitlines() if line.startswith("data: ")][-1])
            assert result["knowledge_scope"]["base_ids"] == ids
            assert result["sources"] and all(s["metadata"]["document_id"] in allowed for s in result["sources"])
            assert all(step["node"] not in {"web", "direct"} for step in result["trace"])
            update = client.request("PATCH", f"/conversations/{cid}/scope", json={"base_ids": ids})
            update.raise_for_status()
            assert update.json()["base_ids"] == ids
            history = client.get(f"/conversations/{cid}/messages").json()
            assert len(history) == 2 and history[-1]["details"]["knowledge_scope"]["base_ids"] == ids
            assert client.post("/knowledge/search", json={"query": "Milvus", "base_ids": []}).status_code == 422
            assert client.post(f"/conversations/{cid}/stream", json={"question": "Milvus", "base_ids": ["missing"]}).status_code == 404
            report.update(status="passed", signed_proxy="passed", scoped_search="passed", scoped_sse="passed", persisted_scope="passed", invalid_scope="passed", citations=len(result["sources"]))
        finally:
            client.delete(f"/conversations/{cid}").raise_for_status()
            client.post("/auth/logout").raise_for_status()
    with httpx.Client() as client:
        assert client.get("http://127.0.0.1:8000/api/v1/health/ready").json()["status"] == "ok"
        assert client.get("http://127.0.0.1:5173/").status_code == 200
    target = ROOT / "test-results/knowledge-scope-http.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
