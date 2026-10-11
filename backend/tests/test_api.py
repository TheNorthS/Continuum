import json

from app.main import create_app
from tests.signed_client import SignedClient as TestClient

PREFIX = "/api/v1"


def upload(client, admin, sample):
    result = client.post(PREFIX + "/knowledge/documents", headers=admin, files={"file": ("chip.md", sample)})
    assert result.status_code == 201, result.text
    return result.json()


def conversation(client, headers):
    return client.post(PREFIX + "/conversations", headers=headers, json={}).json()["id"]


def test_document_chat_cache_invalidation_and_persistence(client, admin, sample, settings):
    doc = upload(client, admin, sample)
    assert doc["chunk_count"] == 2
    assert upload(client, admin, sample)["duplicate"] is True
    assert (
        client.post(
            PREFIX + "/knowledge/documents", headers=admin, files={"file": ("same-content.exe", sample)}
        ).status_code
        == 415
    )
    search = {"query": "EUV 光刻波长", "top_k": 2}
    hits = client.post(PREFIX + "/knowledge/search", headers=admin, json=search).json()["results"]
    assert hits and "13.5" in hits[0]["text"]
    cid = conversation(client, admin)
    result = client.post(
        f"{PREFIX}/conversations/{cid}/messages", headers=admin, json={"question": "EUV 光刻的波长是多少？"}
    )
    assert result.status_code == 200, result.text
    assert "13.5" in result.json()["answer"]
    assert result.json()["status"] == "demo"
    assert result.json()["sources"][0]["metadata"]["filename"] == "chip.md"
    history = client.get(f"{PREFIX}/conversations/{cid}/messages", headers=admin).json()
    assert [m["role"] for m in history] == ["user", "assistant"]
    with TestClient(create_app(settings)) as reopened:
        assert len(reopened.get(f"{PREFIX}/conversations/{cid}/messages", headers=admin).json()) == 2
        assert len(reopened.get(PREFIX + "/knowledge/documents", headers=admin).json()) == 1
    assert client.delete(f"{PREFIX}/knowledge/documents/{doc['id']}", headers=admin).status_code == 204
    assert client.post(PREFIX + "/knowledge/search", headers=admin, json=search).json()["results"] == []


def test_ownership_and_admin_permissions(client, admin):
    assert client.get(PREFIX + "/conversations").status_code == 401
    assert (
        client.post(
            PREFIX + "/auth/register", json={"username": "alice", "password": "alice-pass-123"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            PREFIX + "/auth/register", json={"username": "alice", "password": "alice-pass-123"}
        ).status_code
        == 409
    )
    login = client.post(
        PREFIX + "/auth/login", json={"username": "alice", "password": "alice-pass-123"}
    ).json()
    other = {"Authorization": "Bearer " + login["access_token"]}
    cid = conversation(client, admin)
    assert client.get(f"{PREFIX}/conversations/{cid}/messages", headers=other).status_code == 404
    assert (
        client.post(
            f"{PREFIX}/conversations/{cid}/stream", headers=other, json={"question": "你好"}
        ).status_code
        == 404
    )
    assert client.delete(f"{PREFIX}/conversations/{cid}", headers=other).status_code == 404
    assert client.get(PREFIX + "/admin/stats", headers=other).status_code == 403
    assert (
        client.post(
            PREFIX + "/knowledge/documents", headers=other, files={"file": ("test.md", b"test")}
        ).status_code
        == 404
    )
    assert client.get(PREFIX + "/metrics", headers=other).status_code == 403
    assert (
        client.post(PREFIX + "/auth/login", json={"username": "admin", "password": "incorrect"}).status_code
        == 401
    )


def test_validation_and_operational_endpoints(client, admin):
    assert client.get(PREFIX + "/health/live").json()["status"] == "ok"
    assert client.get(PREFIX + "/health/ready").status_code == 200
    assert "rag_e_http_requests_total" in client.get(PREFIX + "/metrics", headers=admin).text
    assert "grade_answer" in client.get(PREFIX + "/agents/workflow", headers=admin).json()["mermaid"]
    for filename, content, status in [
        ("empty.md", b"", 422),
        ("code.exe", b"abc", 415),
        ("bad.md", b"\xff", 422),
    ]:
        assert (
            client.post(
                PREFIX + "/knowledge/documents", headers=admin, files={"file": (filename, content)}
            ).status_code
            == status
        )
    cid = conversation(client, admin)
    response = client.post(f"{PREFIX}/conversations/{cid}/messages", headers=admin, json={"question": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert (
        client.post(
            PREFIX + "/knowledge/search", headers=admin, json={"query": "a", "top_k": 999}
        ).status_code
        == 422
    )


def test_sse_progress_final_and_error(client, admin, sample, monkeypatch):
    upload(client, admin, sample)
    cid = conversation(client, admin)
    response = client.post(
        f"{PREFIX}/conversations/{cid}/stream", headers=admin, json={"question": "EUV 光刻"}
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(frames) >= 5
    assert json.loads(frames[-1])["conversation_id"] == cid
    assert "event: result" in response.text

    async def fail(*args, **kwargs):
        raise RuntimeError("private provider response")

    monkeypatch.setattr(client.app.state.container.orchestrator, "run", fail)
    response = client.post(
        f"{PREFIX}/conversations/{cid}/stream", headers=admin, json={"question": "失败测试"}
    )
    assert "event: error" in response.text
    assert "private provider" not in response.text
    assert len(client.get(f"{PREFIX}/conversations/{cid}/messages", headers=admin).json()) == 2
    # A failed stream must release the shared conversation lease.
    assert client.delete(f"{PREFIX}/conversations/{cid}", headers=admin).status_code == 204


def test_concurrent_turns_are_rejected(client, admin):
    cid = conversation(client, admin)
    services = client.app.state.container
    uid = client.get(PREFIX + "/auth/me", headers=admin).json()["id"]
    token = services.repository.claim(cid, uid)
    try:
        assert (
            client.post(
                f"{PREFIX}/conversations/{cid}/messages", headers=admin, json={"question": "你好"}
            ).status_code
            == 409
        )
        assert client.delete(f"{PREFIX}/conversations/{cid}", headers=admin).status_code == 409
    finally:
        services.repository.release(cid, token)
    assert (
        client.post(
            f"{PREFIX}/conversations/{cid}/messages",
            headers=admin,
            json={"question": "你好", "mode": "agentic"},
        ).json()["status"]
        == "direct"
    )


def test_upload_limit_and_rate_limit(settings):
    settings.max_upload_bytes = 1024
    settings.rate_limit_per_minute = 4  # Includes the mandatory login challenge request.
    with TestClient(create_app(settings)) as c:
        auth = c.post(
            PREFIX + "/auth/login", json={"username": "admin", "password": "rag-e-demo-2026"}
        ).json()
        headers = {"Authorization": "Bearer " + auth["access_token"]}
        assert (
            c.post(
                PREFIX + "/knowledge/documents", headers=headers, files={"file": ("large.md", b"a" * 1025)}
            ).status_code
            == 413
        )
        assert c.get(PREFIX + "/system", headers=headers).status_code == 200
        assert c.get(PREFIX + "/system", headers=headers).status_code == 200
        assert c.get(PREFIX + "/system", headers=headers).status_code == 429
        assert c.get(PREFIX + "/health/live").status_code == 200
