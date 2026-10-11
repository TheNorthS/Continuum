"""Signed HTTP rerank acceptance; creates and deletes only its own temporary data."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.security_client import SecurityClient


def main():
    with SecurityClient("http://127.0.0.1:5173/api/v1", timeout=180) as client:
        client.login(os.getenv("RAGE_TEST_ADMIN_USERNAME", "admin"), os.getenv("RAGE_TEST_ADMIN_PASSWORD", "rag-e-demo-2026"))
        doc_id = cid = None
        try:
            state = client.get("/system").json()["reranker"]
            assert state["enabled"]
            base = next(b for b in client.get("/knowledge/bases").json() if b["access"] != "read")
            text = "# 精排验收\n\n## 数据库\nPostgreSQL 是关系数据库。\n\n## EUV 波长\nEUV 光刻采用 13.5 纳米波长的极紫外光。\n\n## 芯片封装\n封装保护裸片并提供散热通道。"
            doc = client.post("/knowledge/documents", data={"base_id": base["id"]}, files={"file": ("rerank-check-" + uuid.uuid4().hex + ".md", text.encode())})
            doc.raise_for_status()
            assert not doc.json().get("duplicate"), "Refusing to remove a pre-existing document"
            doc_id = doc.json()["id"]
            started = time.perf_counter()
            result = client.post("/knowledge/search", json={"query": "EUV 波长是多少？", "base_ids": [base["id"]], "top_k": 2})
            result.raise_for_status()
            hits = result.json()["results"]
            assert hits and all(h["metadata"]["rerank_status"] == "ranked" for h in hits)
            assert all(0 <= h["rerank_score"] <= 1 for h in hits)
            search_seconds = time.perf_counter() - started
            conversation = client.post("/conversations", json={"title": "精排临时验收", "base_ids": [base["id"]]})
            conversation.raise_for_status()
            cid = conversation.json()["id"]
            stream = client.post(f"/conversations/{cid}/stream", json={"question": "EUV 波长是多少？"})
            stream.raise_for_status()
            assert "event: result" in stream.text
            answer = json.loads([line[6:] for line in stream.text.splitlines() if line.startswith("data: ")][-1])
            assert answer["sources"] and all(s["metadata"]["rerank_status"] == "ranked" for s in answer["sources"])
            snapshot = client.get("/admin/observability").json()
            assert snapshot["profiler"]["rerank"]["count"] > 0
            assert snapshot["reranker"]["state"] == "ready"
            metrics = client.get("/metrics").text
            assert 'operation="rerank"' in metrics
            report = {"status": "passed", "model": state["model"], "provider": state["provider"], "search_seconds": round(search_seconds, 3), "sources": len(answer["sources"]), "sse_ranked_sources": True, "profiler": True, "metrics": True}
            (ROOT / "test-results/rerank-http.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
        finally:
            if cid:
                client.delete(f"/conversations/{cid}").raise_for_status()
            if doc_id:
                client.delete(f"/knowledge/documents/{doc_id}").raise_for_status()
            client.post("/auth/logout").raise_for_status()


if __name__ == "__main__":
    main()
