import asyncio
import json
import os
import uuid

import pytest
from pydantic import SecretStr

from app.common.context import knowledge_scope
from app.main import create_app
from tests.signed_client import SignedClient
from tests.test_workflow import ScriptedLLM, build

P = "/api/v1"


def base(client, admin, name):
    response = client.post(P + "/knowledge/bases", headers=admin, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def document(client, admin, bid, marker):
    response = client.post(
        P + "/knowledge/documents",
        headers=admin,
        data={"base_id": bid},
        files={
            "file": (
                marker + ".md",
                f"# EUV 光刻\nEUV 光刻采用 13.5 纳米波长。本知识库的项目名称是{marker}。".encode(),
            )
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create(client, admin, ids=None):
    response = client.post(P + "/conversations", headers=admin, json={"base_ids": ids})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_single_multi_search_cache_and_chat_scope_persist(client, admin, settings):
    a, b, c = [base(client, admin, name) for name in ("研发资料", "生产资料", "其他资料")]
    docs = {
        bid: document(client, admin, bid, name)
        for bid, name in ((a, "研发一号"), (b, "生产二号"), (c, "其他三号"))
    }
    # Warm all-accessible cache first; repeat overlapping scopes and confirm cache isolation.
    for ids in (None, [a], [b], [a, b], [a], [c], [b, a]):
        response = client.post(
            P + "/knowledge/search", headers=admin, json={"query": "EUV 光刻", "top_k": 12, "base_ids": ids}
        )
        assert response.status_code == 200, response.text
        found = {s["metadata"]["document_id"] for s in response.json()["results"]}
        assert found == {docs[i] for i in ids or (a, b, c)}
    cid = create(client, admin, [a])
    for endpoint in ("messages", "stream"):
        response = client.post(
            f"{P}/conversations/{cid}/{endpoint}", headers=admin, json={"question": "EUV 光刻波长是多少？"}
        )
        assert response.status_code == 200, response.text
        result = (
            response.json()
            if endpoint == "messages"
            else json.loads(
                [line[6:] for line in response.text.splitlines() if line.startswith("data: ")][-1]
            )
        )
        assert result["knowledge_scope"]["base_ids"] == [a]
        assert result["knowledge_scope"]["base_names"] == ["研发资料"]
        assert {s["metadata"]["document_id"] for s in result["sources"]} == {docs[a]}
    with SignedClient(create_app(settings)) as reopened:
        assert reopened.get(f"{P}/conversations/{cid}", headers=admin).json()["base_ids"] == [a]
        assert len(reopened.get(f"{P}/conversations/{cid}/messages", headers=admin).json()) == 4


def test_scope_change_resets_model_context_preserves_display_history(client, admin, monkeypatch):
    a, b = base(client, admin, "库一"), base(client, admin, "库二")
    document(client, admin, a, "一号")
    cid = create(client, admin, [a])
    assert (
        client.post(
            f"{P}/conversations/{cid}/messages", headers=admin, json={"question": "EUV 波长"}
        ).status_code
        == 200
    )
    observed = []

    async def run(question, history, mode, emit=None):
        observed.append((history, knowledge_scope.get()))
        return {
            "answer": "资料不足。",
            "sources": [],
            "trace": [],
            "status": "insufficient",
            "mode": mode,
            "runtime": "demo",
            "rewrites": 0,
            "generations": 0,
        }

    monkeypatch.setattr(client.app.state.container.orchestrator, "run", run)
    changed = client.patch(f"{P}/conversations/{cid}/scope", headers=admin, json={"base_ids": [b]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["scope_start_id"] > 0
    assert len(client.get(f"{P}/conversations/{cid}/messages", headers=admin).json()) == 2
    for body in (
        {"question": "新库问题"},
        {"question": "追问", "base_ids": [b, b]},
        {"question": "回到原库", "base_ids": [a]},
    ):
        assert client.post(f"{P}/conversations/{cid}/messages", headers=admin, json=body).status_code == 200
    assert observed[0] == ([], (b,))
    assert len(observed[1][0]) == 2
    assert observed[2] == ([], (a,))
    assert len(client.get(f"{P}/conversations/{cid}/messages", headers=admin).json()) == 8


def test_empty_unknown_and_unauthorized_scope_never_expand(client, admin):
    cid = create(client, admin)
    for ids, status in (([], 422), (["missing"], 404)):
        for path, body in (
            ("/knowledge/search", {"query": "EUV"}),
            ("/conversations", {}),
            (f"/conversations/{cid}/messages", {"question": "EUV"}),
            (f"/conversations/{cid}/stream", {"question": "EUV"}),
        ):
            response = client.post(P + path, headers=admin, json={**body, "base_ids": ids})
            assert response.status_code == status, response.text
        assert (
            client.patch(f"{P}/conversations/{cid}/scope", headers=admin, json={"base_ids": ids}).status_code
            == status
        )
    private = base(client, admin, "私有库")
    client.post(P + "/auth/register", json={"username": "scope-user", "password": "scope-password-123"})
    login = client.post(
        P + "/auth/login", json={"username": "scope-user", "password": "scope-password-123"}
    ).json()
    other = {"Authorization": "Bearer " + login["access_token"]}
    assert (
        client.post(
            P + "/knowledge/search", headers=other, json={"query": "EUV", "base_ids": [private]}
        ).status_code
        == 404
    )
    assert client.get(f"{P}/conversations/{cid}", headers=other).status_code == 404
    assert (
        client.patch(
            f"{P}/conversations/{cid}/scope", headers=other, json={"base_ids": [private]}
        ).status_code
        == 404
    )


def test_scope_edit_during_generation_rejected(client, admin):
    a = base(client, admin, "库一")
    cid = create(client, admin)
    uid = client.get(P + "/auth/me", headers=admin).json()["id"]
    repo = client.app.state.container.repository
    token = repo.claim(cid, uid)
    try:
        assert (
            client.patch(f"{P}/conversations/{cid}/scope", headers=admin, json={"base_ids": [a]}).status_code
            == 409
        )
    finally:
        repo.release(cid, token)
    assert client.get(f"{P}/conversations/{cid}", headers=admin).json()["base_ids"] is None


@pytest.mark.parametrize("mode", ["adaptive", "agentic"])
@pytest.mark.parametrize("route", ["web", "direct"])
async def test_selected_scope_forces_retrieval_without_web_fallback(settings, mode, route):
    settings.web_search_enabled = True
    flow, knowledge, web = build(settings, ScriptedLLM(route=route), sources=[])
    token = knowledge_scope.set(("chosen",))
    try:
        result = await flow.run("问题", [], mode)
        assert result["status"] == "insufficient"
        assert knowledge.search.await_count > 0
        web.search.assert_not_awaited()
    finally:
        knowledge_scope.reset(token)


async def test_scope_is_task_local_and_thread_safe(client, admin):
    a, b = base(client, admin, "库一"), base(client, admin, "库二")
    da, db = document(client, admin, a, "一号"), document(client, admin, b, "二号")
    repo = client.app.state.container.repository

    async def documents_in_scope(ids):
        token = knowledge_scope.set(ids)
        try:
            await asyncio.sleep(0)
            return {d["id"] for d in await asyncio.to_thread(repo.documents)}
        finally:
            knowledge_scope.reset(token)

    assert await asyncio.gather(documents_in_scope((a,)), documents_in_scope((b,))) == [{da}, {db}]
    assert knowledge_scope.get() is None


def test_revoked_saved_library_rejects_next_turn_without_widening(client, admin):
    from tests.test_security_complete import headers, register

    shared = base(client, admin, "共享给单个用户")
    user = register(client, "scope-reader")
    reader = headers(user)
    grant = {"subject_type": "user", "subject_id": user["user"]["id"], "access": "read"}
    path = f"{P}/knowledge/bases/{shared}/grants"
    assert client.put(path, headers=admin, json=grant).status_code == 204
    cid = create(client, reader, [shared])
    grant["access"] = None
    assert client.put(path, headers=admin, json=grant).status_code == 204
    for endpoint in ("messages", "stream"):
        response = client.post(
            f"{P}/conversations/{cid}/{endpoint}", headers=reader, json={"question": "EUV"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "scope_unavailable"
    assert client.get(f"{P}/conversations/{cid}/messages", headers=reader).json() == []


def test_final_response_gate_rejects_sources_outside_selection(client, admin, monkeypatch):
    a, b = base(client, admin, "选中库"), base(client, admin, "未选中库")
    doc = document(client, admin, b, "其他资料")
    cid = create(client, admin, [a])

    async def faulty_retrieval(*args, **kwargs):
        return {
            "answer": "回答",
            "sources": [{"id": f"{doc}_0", "text": "其他资料", "metadata": {}}],
            "rewrites": 0,
            "generations": 0,
        }

    monkeypatch.setattr(client.app.state.container.orchestrator, "run", faulty_retrieval)
    response = client.post(f"{P}/conversations/{cid}/messages", headers=admin, json={"question": "资料查询"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "source_scope_changed"
    assert client.get(f"{P}/conversations/{cid}/messages", headers=admin).json() == []


@pytest.mark.external
@pytest.mark.skipif(not os.getenv("RAGE_TEST_NEO4J_URI"), reason="Neo4j test service not configured")
def test_selected_scope_with_real_graph_and_redis_l2(settings):
    settings.graph_enabled = True
    settings.neo4j_uri = os.environ["RAGE_TEST_NEO4J_URI"]
    settings.neo4j_password = SecretStr(os.getenv("RAGE_TEST_NEO4J_PASSWORD", "rag-e-local-neo4j"))
    settings.redis_url = SecretStr(os.getenv("RAGE_TEST_REDIS_URL", ""))
    settings.knowledge_namespace = "scope_" + uuid.uuid4().hex
    with SignedClient(create_app(settings)) as client:
        auth = client.post(
            P + "/auth/login", json={"username": "admin", "password": "rag-e-demo-2026"}
        ).json()
        admin = {"Authorization": "Bearer " + auth["access_token"]}
        docs = {}
        try:
            for name in ("研发", "产品", "经营"):
                bid = base(client, admin, name)
                response = client.post(
                    P + "/knowledge/documents",
                    headers=admin,
                    data={"base_id": bid},
                    files={"file": (name + ".md", f"共同实体 --关联--> {name}实体".encode())},
                )
                assert response.status_code == 201, response.text
                docs[bid] = response.json()["id"]
                assert response.json()["graph_status"] == "ready"
            a, b, c = list(docs)
            services = client.app.state.container
            for ids in (None, [a], [b], [a, b], [c], [a]):
                for clear_l1 in (False, True):
                    if clear_l1:
                        client.portal.call(services.cache.local.clear)
                    before = services.cache.counts["l2_hit"]
                    response = client.post(
                        P + "/knowledge/search",
                        headers=admin,
                        json={"query": "共同实体", "top_k": 12, "base_ids": ids},
                    )
                    assert response.status_code == 200, response.text
                    results = response.json()["results"]
                    assert {r["metadata"]["document_id"] for r in results} == {
                        docs[i] for i in ids or (a, b, c)
                    }
                    assert all(r["metadata"].get("retrieval") == "graph" for r in results)
                    if clear_l1 and services.cache.redis:
                        assert services.cache.counts["l2_hit"] > before
        finally:
            for doc_id in docs.values():
                client.delete(f"{P}/knowledge/documents/{doc_id}", headers=admin).raise_for_status()

            async def cleanup():
                async with client.app.state.container.graph.store.driver.session() as session:
                    await (
                        await session.run(
                            "MATCH (g:RageGraph {namespace:$ns}) DELETE g", ns=settings.knowledge_namespace
                        )
                    ).consume()

            client.portal.call(cleanup)
