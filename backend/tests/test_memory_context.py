import asyncio
import json

import httpx
import pytest
from sqlalchemy import text, update

from app.common.errors import AppError
from app.memory.context import projected
from app.memory.policy import closed_segments
from app.models.entities import (
    Conversation,
    Message,
)
from tests.test_security_complete import headers, register

P = "/api/v1"


def create(client, auth):
    return client.post(P + "/conversations", headers=auth, json={"use_knowledge": False}).json()["id"]


def seed(services, cid, turns=8, size=1200):
    with services.db.session.begin() as session:
        for i in range(turns):
            session.add(
                Message(
                    conversation_id=cid,
                    role="user",
                    content=f"目标{i}：保留兼容性并完成测试。" + "项目资料 " * (size // 5),
                )
            )
            session.add(
                Message(
                    conversation_id=cid,
                    role="assistant",
                    content=f"第{i}轮状态：正在验证。" + "已读取资料 " * (size // 6),
                )
            )


def memory_input(value="PostgreSQL", **values):
    return {"subject": "青禾项目", "predicate": "数据库", "value": value, "category": "fact", **values}


def test_explicit_memory_versions_evidence_privacy_forget_and_export(client, admin):
    first = client.post(P + "/memory", headers=admin, json=memory_input())
    assert first.status_code == 201, first.text
    mid = first.json()["id"]
    data = client.get(P + "/memory", headers=admin).json()
    assert data["items"][0]["payload"]["value"] == "PostgreSQL"
    assert "embedding" not in data["items"][0]
    other = headers(register(client, "memory-other"))
    assert client.get(P + "/memory", headers=other).json()["items"] == []
    assert client.get(P + f"/memory/{mid}/evidence", headers=other).status_code == 404
    assert client.delete(P + f"/memory/{mid}", headers=other).status_code == 404
    assert client.get(P + f"/memory/{mid}/evidence", headers=admin).json()["events"][0]["role"] == "user"
    corrected = client.patch(
        P + f"/memory/{mid}", headers=admin, json={**memory_input("MySQL"), "expected_version": 1}
    )
    assert corrected.status_code == 200 and corrected.json()["version"] == 2
    assert (
        client.patch(
            P + f"/memory/{mid}", headers=admin, json={**memory_input("old"), "expected_version": 1}
        ).status_code
        == 409
    )
    values = client.get(P + "/memory", headers=admin).json()["items"]
    assert {m["status"] for m in values} == {"active", "superseded"}
    exported = client.get(P + "/memory/export/markdown", headers=admin)
    assert "MySQL" in exported.text and "PostgreSQL" not in exported.text
    assert exported.headers["cache-control"] == "no-store"
    services = client.app.state.container
    with services.db.engine.connect() as conn:
        raw = conn.execute(text("SELECT payload,embedding FROM memory_records")).all()
        assert all(r.payload.startswith("rage1:") and r.embedding.startswith("rage1:") for r in raw)
    client.delete(P + f"/memory/{mid}", headers=admin).raise_for_status()
    assert "MySQL" not in client.get(P + "/memory/export/markdown", headers=admin).text
    assert all(r["payload"] == {} for r in client.get(P + "/memory", headers=admin).json()["items"])


def test_sensitive_memory_and_untrusted_data_cannot_be_stored(client, admin):
    for value in ("password=private-value-123", "sk-abcdefghijk123456789", "user@example.com"):
        response = client.post(P + "/memory", headers=admin, json=memory_input(value))
        assert response.status_code == 422 and value not in response.text
    assert client.get(P + "/memory", headers=admin).json()["items"] == []


def test_memory_recall_injection_stays_in_user_payload_and_off_switch_works(client, admin, monkeypatch):
    client.post(P + "/memory", headers=admin, json=memory_input()).raise_for_status()
    services = client.app.state.container
    original = services.llm._call
    seen = []

    async def capture(task, payload, observation=None):
        if task == "general":
            seen.append(payload)
        return await original(task, payload, observation)

    monkeypatch.setattr(services.llm, "_call", capture)
    cid = create(client, admin)
    response = client.post(
        P + f"/conversations/{cid}/messages", headers=admin, json={"question": "青禾项目数据库是什么"}
    )
    assert response.status_code == 200, response.text
    assert seen[-1]["memory_context"]["records"][0]["text"].find("PostgreSQL") >= 0
    assert response.json()["context"]["memory_ids"]
    body = services.llm.body("general", seen[-1])
    assert "PostgreSQL" not in body["messages"][0]["content"]
    assert "PostgreSQL" in body["messages"][1]["content"]
    client.patch(P + "/memory/preferences", headers=admin, json={"enabled": False}).raise_for_status()
    cid = create(client, admin)
    assert (
        client.post(
            P + f"/conversations/{cid}/messages", headers=admin, json={"question": "青禾项目数据库"}
        ).status_code
        == 200
    )
    assert "memory_context" not in seen[-1]


def test_base_scope_and_revocation_block_memory_before_retrieval(client, admin):
    bid = client.post(P + "/knowledge/bases", headers=admin, json={"name": "private"}).json()["id"]
    reader = register(client, "memory-reader")
    auth = headers(reader)
    grant = {"subject_type": "user", "subject_id": reader["user"]["id"], "access": "read"}
    client.put(P + f"/knowledge/bases/{bid}/grants", headers=admin, json=grant).raise_for_status()
    result = client.post(P + "/memory", headers=auth, json=memory_input(base_id=bid))
    assert result.status_code == 201, result.text
    services = client.app.state.container

    async def retrieve(enabled):
        return await services.memory_retriever.search(
            reader["user"]["id"],
            "青禾项目数据库",
            {"use_knowledge": enabled, "base_ids": [bid] if enabled else None, "attachment_ids": []},
        )

    assert client.portal.call(retrieve, True)
    assert client.portal.call(retrieve, False) == []
    grant["access"] = None
    client.put(P + f"/knowledge/bases/{bid}/grants", headers=admin, json=grant).raise_for_status()
    assert client.portal.call(retrieve, True) == []
    assert client.get(P + "/memory", headers=auth).json()["items"] == []


def test_compaction_preserves_raw_messages_restarts_and_uses_recent_tail(client, admin, monkeypatch):
    services = client.app.state.container
    cid = create(client, admin)
    seed(services, cid)
    before = client.get(P + f"/conversations/{cid}/messages", headers=admin).json()
    compact = client.post(P + f"/conversations/{cid}/context/compact", headers=admin)
    assert compact.status_code == 200 and compact.json()["committed"], compact.text
    current = client.get(P + f"/conversations/{cid}/context", headers=admin).json()
    cp = current["checkpoint"]
    assert cp["tokens_after"] < cp["tokens_before"]
    assert cp["summary"]["original_goal"]["text"] == before[0]["content"]
    assert len(client.get(P + f"/conversations/{cid}/messages", headers=admin).json()) == len(before)
    uid = services.repository.user(username="admin")["id"]
    snapshot = services.memory_store.snapshot(cid, uid)
    history = services.context.history({"snapshot": snapshot})
    assert history[-1]["content"] == before[-1]["content"]
    assert "历史压缩检查点" in history[0]["content"]
    assert not any(m["content"] == before[0]["content"] for m in history[1:])
    from app.main import create_app
    from tests.signed_client import SignedClient

    with SignedClient(create_app(services.settings)) as reopened:
        assert (
            reopened.get(P + f"/conversations/{cid}/context", headers=admin).json()["checkpoint"]["id"]
            == cp["id"]
        )


def test_pinned_messages_bad_summary_and_revision_conflict_never_replace_history(client, admin, monkeypatch):
    services = client.app.state.container
    cid = create(client, admin)
    seed(services, cid)
    status = client.get(P + f"/conversations/{cid}/context", headers=admin).json()
    first = status["events"][0]["id"]
    client.put(
        P + f"/conversations/{cid}/context/pins", headers=admin, json={"event_ids": [first]}
    ).raise_for_status()
    assert not client.post(P + f"/conversations/{cid}/context/compact", headers=admin).json()["committed"]
    client.put(
        P + f"/conversations/{cid}/context/pins", headers=admin, json={"event_ids": []}
    ).raise_for_status()
    original = services.llm.call

    async def invalid(task, payload):
        value = await original(task, payload)
        if task == "memory_compact":
            value["current_status"]["evidence_ids"] = ["invented"]
        return value

    monkeypatch.setattr(services.llm, "call", invalid)
    assert not client.post(P + f"/conversations/{cid}/context/compact", headers=admin).json()["committed"]
    monkeypatch.setattr(services.llm, "call", original)
    uid = services.repository.user(username="admin")["id"]
    token = services.repository.claim(cid, uid)
    try:
        snap = services.memory_store.snapshot(cid, uid)
        with services.db.session.begin() as session:
            session.execute(
                update(Conversation)
                .where(Conversation.id == cid)
                .values(context_revision=Conversation.context_revision + 1)
            )
        with pytest.raises(AppError, match="变化"):
            services.memory_store.commit_checkpoint(snap, {}, snap["events"][:2], 100, 20, token)
    finally:
        services.repository.release(cid, token)
    assert client.get(P + f"/conversations/{cid}/context", headers=admin).json()["checkpoint"] is None


def test_event_pairing_reference_pruning_and_artifact_readback(client, admin):
    from tests.test_attachments import upload

    services = client.app.state.container
    aid = upload(client, admin).json()["id"]
    cid = client.post(
        P + "/conversations", headers=admin, json={"use_knowledge": False, "attachment_ids": [aid]}
    ).json()["id"]
    client.post(
        P + f"/conversations/{cid}/messages", headers=admin, json={"question": "EUV 光刻"}
    ).raise_for_status()
    uid = services.repository.user(username="admin")["id"]
    snap = services.memory_store.snapshot(cid, uid)
    segments, tail = closed_segments(snap["events"])
    assert len(segments) == 1 and not tail
    tool = next(e for e in snap["events"] if e["kind"] == "tool_result")
    projection = projected(snap["events"])
    assert projection[-1]["artifact_refs"][0]["artifact_ref"] == tool["content"]["artifact_ref"]
    artifact = tool["content"]["artifact_ref"].removeprefix("artifact://")
    assert client.get(P + f"/memory/artifacts/{artifact}", headers=admin).json()["sources"][0]["text"]
    client.delete(P + f"/attachments/{aid}", headers=admin).raise_for_status()
    assert client.get(P + f"/memory/artifacts/{artifact}", headers=admin).status_code == 404
    assert services.memory_store.snapshot(cid, uid)["events"] == []


def test_token_meter_counts_full_request_and_rejects_invalid_provider(settings):
    from app.memory.tokens import TokenMeter

    settings.context_token_meter = "provider"
    settings.context_count_url = "https://count.example/tokens"
    body = {
        "model": "demo",
        "messages": [{"role": "user", "content": "中文"}],
        "tools": [{"type": "function"}],
        "max_tokens": 1600,
    }

    async def check():
        def handler(request):
            payload = json.loads(request.content)
            assert payload["messages"] == body["messages"] and payload["tools"] == body["tools"]
            return httpx.Response(200, json={"input_tokens": 123})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await TokenMeter(settings, http).count(body) == 123
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"input_tokens": "123"}))
        ) as http:
            with pytest.raises(AppError):
                await TokenMeter(settings, http).count(body)

    asyncio.run(check())


@pytest.mark.parametrize(
    "finish,content,valid",
    [
        ("stop", {"memories": []}, True),
        ("length", {"memories": []}, False),
        ("stop", {"memories": [], "unexpected": "private-data"}, False),
        (
            "stop",
            {
                "memories": [
                    {
                        "category": "fact",
                        "subject": "project",
                        "predicate": "database",
                        "value": "PostgreSQL",
                        "evidence_ids": ["event"],
                        "quote": "PostgreSQL",
                        "evidence_type": "user_explicit",
                        "confidence": 0.9,
                        "reuse": True,
                        "frequency": 1.0,
                        "stability": 1.0,
                        "novelty": 1.0,
                    }
                ]
            },
            False,
        ),
    ],
)
async def test_live_memory_extraction_protocol_is_strict(settings, finish, content, valid):
    from app.services.llm_service import LLMService

    settings.mode = "live"

    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert "events" in json.loads(body["messages"][-1]["content"])
        return httpx.Response(
            200, json={"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        service = LLMService(settings, http)
        if valid:
            assert await service.call("memory_extract", {"events": []}) == content
        else:
            with pytest.raises(AppError) as error:
                await service.call("memory_extract", {"events": []})
            assert error.value.code == "llm_format" and "private-data" not in str(error.value)


def test_worker_two_phase_idempotency_conflict_forget_and_lease_recovery(client, admin, monkeypatch):
    services = client.app.state.container
    cid = create(client, admin)
    with services.db.session.begin() as s:
        s.add_all(
            [
                Message(conversation_id=cid, role="user", content="青禾项目数据库使用 PostgreSQL。"),
                Message(conversation_id=cid, role="assistant", content="已了解。"),
            ]
        )
    uid = services.repository.user(username="admin")["id"]
    original = services.llm.call

    async def extract(task, payload):
        if task == "memory_extract":
            e = payload["events"][0]
            return {
                "memories": [
                    {
                        "subject": "青禾项目",
                        "predicate": "数据库",
                        "value": "PostgreSQL",
                        "category": "fact",
                        "quote": "数据库使用 PostgreSQL",
                        "evidence_ids": [e["id"]],
                        "evidence_type": "user_explicit",
                        "confidence": 0.9,
                        "reuse": 1.0,
                        "frequency": 1.0,
                        "stability": 1.0,
                        "novelty": 1.0,
                    }
                ]
            }
        return await original(task, payload)

    monkeypatch.setattr(services.llm, "call", extract)
    queued = client.post(P + f"/conversations/{cid}/memory/extract", headers=admin)
    assert queued.status_code == 202
    assert client.portal.call(services.memory.run_once)
    active = [r for r in client.get(P + "/memory", headers=admin).json()["items"] if r["status"] == "active"]
    assert len(active) == 1
    assert not client.portal.call(services.memory.run_once)
    client.delete(P + f"/memory/{active[0]['id']}", headers=admin).raise_for_status()
    client.post(P + f"/conversations/{cid}/memory/extract", headers=admin).raise_for_status()
    assert client.portal.call(services.memory.run_once)
    assert not services.memory_store.list_records(uid)


def test_context_overflow_classification_only_retries_confirmed_codes():
    from app.services.llm_service import context_overflow

    for status, code, expected in (
        (400, "context_length_exceeded", True),
        (429, "rate_limit", False),
        (401, "unauthorized", False),
        (400, "invalid_model", False),
    ):
        request = httpx.Request("POST", "https://example.invalid/chat/completions")
        response = httpx.Response(status, request=request, json={"error": {"code": code}})
        error = AppError("upstream_error", "safe", 502)
        error.__cause__ = httpx.HTTPStatusError("safe", request=request, response=response)
        assert context_overflow(error) is expected


def test_automatic_compaction_enforces_target_and_can_restore_prior_projection(client, admin):
    services = client.app.state.container
    services.settings.context_window = 24000
    services.settings.context_keep_recent = 1500
    services.settings.context_summary_tokens = 1500
    cid = create(client, admin)
    seed(services, cid, turns=10, size=800)
    response = client.post(
        P + f"/conversations/{cid}/messages", headers=admin, json={"question": "继续按原计划进行"}
    )
    assert response.status_code == 200, response.text
    report = response.json()["context"]
    assert report["compactions"] == 1 and report["tokens_after"] < report["tokens_before"]
    cp = report["checkpoint_id"]
    assert report["input_units"] <= services.token_meter.limits(services.settings.llm_model)["target"]
    assert (
        client.post(
            P + f"/conversations/{cid}/context/restore", headers=admin, json={"checkpoint_id": None}
        ).status_code
        == 200
    )
    assert client.get(P + f"/conversations/{cid}/context", headers=admin).json()["checkpoint"] is None
    assert (
        client.post(
            P + f"/conversations/{cid}/context/restore", headers=admin, json={"checkpoint_id": cp}
        ).status_code
        == 200
    )
    assert client.get(P + f"/conversations/{cid}/context", headers=admin).json()["checkpoint"]["id"] == cp


def test_true_overflow_recovers_once_and_auth_error_does_not_compact(client, admin, monkeypatch):
    services = client.app.state.container
    cid = create(client, admin)
    seed(services, cid)
    original = services.llm._dispatch
    calls = []

    async def dispatched(task, payload):
        if task == "general":
            calls.append(payload)
            if len(calls) == 1:
                req = httpx.Request("POST", "https://example.invalid/chat/completions")
                res = httpx.Response(400, request=req, json={"error": {"code": "context_length_exceeded"}})
                err = AppError("upstream_error", "safe", 502)
                err.__cause__ = httpx.HTTPStatusError("safe", request=req, response=res)
                raise err
        return await original(task, payload)

    monkeypatch.setattr(services.llm, "_dispatch", dispatched)
    response = client.post(P + f"/conversations/{cid}/messages", headers=admin, json={"question": "继续"})
    assert response.status_code == 200, response.text
    assert response.json()["context"]["overflow_recovered"] and len(calls) == 2
    assert len(json.dumps(calls[1])) < len(json.dumps(calls[0]))


def test_worker_conflict_does_not_overwrite_and_user_correction_resolves(client, admin, monkeypatch):
    services = client.app.state.container
    client.post(P + "/memory", headers=admin, json=memory_input("MySQL")).raise_for_status()
    cid = create(client, admin)
    with services.db.session.begin() as session:
        session.add_all(
            [
                Message(conversation_id=cid, role="user", content="青禾项目数据库使用 PostgreSQL。"),
                Message(conversation_id=cid, role="assistant", content="收到"),
            ]
        )
    original = services.llm.call

    async def llm(task, payload):
        if task == "memory_extract":
            e = payload["events"][0]
            return {
                "memories": [
                    {
                        **memory_input(),
                        "quote": "数据库使用 PostgreSQL",
                        "evidence_ids": [e["id"]],
                        "evidence_type": "user_explicit",
                        "confidence": 1.0,
                        "reuse": 1.0,
                        "frequency": 1.0,
                        "stability": 1.0,
                        "novelty": 1.0,
                    }
                ]
            }
        return await original(task, payload)

    monkeypatch.setattr(services.llm, "call", llm)
    client.post(P + f"/conversations/{cid}/memory/extract", headers=admin).raise_for_status()
    assert client.portal.call(services.memory.run_once)
    rows = client.get(P + "/memory", headers=admin).json()["items"]
    assert len(rows) == 2 and all(r["status"] == "disputed" for r in rows)
    latest = max(rows, key=lambda r: r["version"])
    corrected = client.patch(
        P + f"/memory/{latest['id']}",
        headers=admin,
        json={**memory_input(), "expected_version": latest["version"]},
    )
    assert corrected.status_code == 200
    active = [r for r in client.get(P + "/memory", headers=admin).json()["items"] if r["status"] == "active"]
    assert len(active) == 1 and active[0]["payload"]["value"] == "PostgreSQL"


def test_worker_lease_takeover_fences_old_worker_and_retries_are_bounded(client, admin):
    import time

    from app.models.entities import MemoryJob

    services = client.app.state.container
    cid = create(client, admin)
    seed(services, cid, turns=2, size=20)
    job = client.post(P + f"/conversations/{cid}/memory/extract", headers=admin).json()
    first = services.memory.claim()
    assert first["id"] == job["id"] and services.memory.claim() is None
    with services.db.session.begin() as session:
        session.get(MemoryJob, first["id"]).lease_until = time.time() - 1
    second = services.memory.claim()
    assert second["token"] != first["token"]
    with pytest.raises(AppError):
        services.memory.stage_candidates(first, [])
    services.memory.stage_candidates(second, [])
    services.memory.consolidate(second, [])
    assert services.memory.jobs(first["owner_id"])[0]["status"] == "completed"


def test_forget_during_generation_blocks_final_commit(client, admin, monkeypatch):
    services = client.app.state.container
    mid = client.post(P + "/memory", headers=admin, json=memory_input()).json()["id"]
    uid = services.repository.user(username="admin")["id"]
    cid = create(client, admin)
    original = services.orchestrator.run

    async def run(*args, **kwargs):
        value = await original(*args, **kwargs)
        services.memory_store.forget(uid, mid)
        return value

    monkeypatch.setattr(services.orchestrator, "run", run)
    response = client.post(
        P + f"/conversations/{cid}/messages", headers=admin, json={"question": "青禾项目数据库"}
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "memory_changed"
    assert client.get(P + f"/conversations/{cid}/messages", headers=admin).json() == []


def test_memory_expiration_filters_retrieval(client, admin):
    import time

    result = client.post(P + "/memory", headers=admin, json=memory_input(expires_at=time.time() - 1))
    assert result.status_code == 201
    uid = client.app.state.container.repository.user(username="admin")["id"]
    assert client.app.state.container.memory_store.list_records(uid) == []


def test_idle_jobs_coalesce_and_explicit_extraction_runs_without_waiting(client, admin):
    import time

    services = client.app.state.container
    services.settings.memory_min_turns = 1
    cid = create(client, admin)
    for question in ("第一轮讨论", "第二轮讨论"):
        client.post(
            P + f"/conversations/{cid}/messages", headers=admin, json={"question": question}
        ).raise_for_status()
    uid = services.repository.user(username="admin")["id"]
    jobs = services.memory.jobs(uid)
    assert len([j for j in jobs if j["status"] == "pending"]) == 1
    assert len([j for j in jobs if j["status"] == "obsolete"]) == 1
    pending = next(j for j in jobs if j["status"] == "pending")
    assert pending["run_after"] > time.time()
    client.post(P + f"/conversations/{cid}/memory/extract", headers=admin).raise_for_status()
    assert client.portal.call(services.memory.run_once)
    assert next(j for j in services.memory.jobs(uid) if j["id"] == pending["id"])["status"] == "completed"


def test_unbalanced_tool_cut_is_rejected():
    with pytest.raises(ValueError, match="orphan"):
        closed_segments([{"id": "e", "kind": "tool_result", "tool_call_id": "missing"}])
    segments, suffix = closed_segments(
        [{"id": "e", "kind": "tool_call", "tool_call_id": "call"}, {"id": "a", "kind": "assistant_message"}]
    )
    assert segments == [] and len(suffix) == 2


def test_migration_upgrade_and_encryption_rotation_include_memory_tables(settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from app.common.encryption import Cipher, encrypt_database
    from app.database.session import Database
    from app.models.entities import MemoryEvent

    cipher = Cipher(settings.encryption_key_file, create=True)
    db = Database(settings.database_url, cipher)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "app/database/migrations"))
    try:
        with db.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0008")
            connection.execute(
                text(
                    "INSERT INTO users (id,username,password_hash,role) VALUES ('u','migration','hash','user')"
                )
            )
            command.upgrade(config, "head")
        with db.session.begin() as session:
            session.add(
                MemoryEvent(
                    id="e",
                    owner_id="u",
                    conversation_id=None,
                    sequence=0,
                    kind="memory_instruction",
                    role="user",
                    content={"text": "encrypted-fact"},
                )
            )
        # Existing pre-security rows in this isolated migration fixture are explicitly encrypted first.
        encrypt_database(db, allow_plaintext=True)
        cipher.rotate()
        encrypt_database(db)
        with db.session() as session:
            assert session.get(MemoryEvent, "e").content["text"] == "encrypted-fact"
        with db.engine.connect() as connection:
            assert (
                connection.execute(text("SELECT content FROM memory_events"))
                .scalar()
                .startswith("rage1:" + cipher.active)
            )
    finally:
        db.close()
