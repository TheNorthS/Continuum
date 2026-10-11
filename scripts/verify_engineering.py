"""HTTP acceptance for the enabled local graph + monitoring workspace."""
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.security_client import SecurityClient


def main():
    report = {}
    with SecurityClient(base_url="http://127.0.0.1:8000/api/v1", timeout=160) as client:
        client.login("admin", "rag-e-demo-2026")
        document = ROOT / "data/sample/knowledge_graph_demo.md"
        response = client.post("/knowledge/documents", files={"file": (document.name, document.read_bytes())})
        response.raise_for_status()
        doc = response.json()
        if doc["graph_status"] != "ready":
            client.post(f"/graph/documents/{doc['id']}/rebuild").raise_for_status()
        graph = client.get("/graph/network")
        graph.raise_for_status()
        network = graph.json()
        assert len(network["nodes"]) >= 15 and len(network["links"]) >= 12
        ids = {n["name"]: n["id"] for n in network["nodes"]}
        path = client.post("/graph/paths", json={"source": ids["哺乳动物"], "target": ids["动物"], "hops": 3})
        path.raise_for_status()
        assert path.json()["inferences"][0]["predicate"] == "子类于"
        client.get("/graph/network").raise_for_status()
        conversation = client.post("/conversations", json={"title": "工程验收临时会话"}).json()
        try:
            response = client.post(f"/conversations/{conversation['id']}/messages", json={"question": "知识助手采用什么技术？"})
            response.raise_for_status()
            assert response.json()["sources"]
            snapshot = client.get("/admin/observability").json()
            spans = [s for s in snapshot["spans"] if s["trace_id"] == response.headers["x-request-id"]]
            assert {"request", "retrieval", "llm", "cypher", "embedding"}.issubset({s["name"] for s in spans})
            assert snapshot["profiler"]["llm"]["p95_ms"] > 0
            assert snapshot["cache"]["events"]["l1_hit"] > 0
            assert snapshot["dependencies"]["neo4j"] and snapshot["dependencies"]["redis"]
            metrics = client.get("/metrics").text
            assert "rag_e_alert_state" in metrics and "rag_e_cache_events_total" in metrics
            report = {"entities": len(network["nodes"]), "relations": len(network["links"]),
                      "subclass_inference": path.json()["inferences"][0], "span_names": sorted({s["name"] for s in spans}),
                      "profiler": snapshot["profiler"], "cache": snapshot["cache"], "dependencies": snapshot["dependencies"],
                      "langfuse": snapshot["tracing"]}
        finally:
            client.delete(f"/conversations/{conversation['id']}").raise_for_status()
    target = ROOT / "test-results/engineering-http.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"entities": report["entities"], "relations": report["relations"],
                      "trace_components": report["span_names"], "status": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
