import json
from pathlib import Path
from unittest.mock import AsyncMock

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.database.session import Database
from app.main import create_app
from tests.signed_client import SignedClient
from tests.test_attachments import P, upload
from tests.test_security_complete import headers, register


def create(client, auth, ids=None):
    response = client.post(
        P + "/conversations", headers=auth, json={"use_knowledge": False, "attachment_ids": ids or []}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def change(client, auth, cid, ids):
    return client.patch(P + f"/conversations/{cid}/attachments", headers=auth, json={"attachment_ids": ids})


def ask(client, auth, cid, stream=False, **options):
    response = client.post(
        P + f"/conversations/{cid}/{'stream' if stream else 'messages'}",
        headers=auth,
        json={"question": "EUV 光刻波长是多少？", **options},
    )
    assert response.status_code == 200, response.text
    return (
        json.loads([line[6:] for line in response.text.splitlines() if line.startswith("data: ")][-1])
        if stream
        else response.json()
    )


def test_saved_selection_survives_restart_and_followups_without_resending_ids(client, admin, settings):
    aid = upload(client, admin).json()["id"]
    cid = create(client, admin, [aid])
    first = ask(client, admin, cid)
    assert first["attachments"][0]["id"] == aid
    with SignedClient(create_app(settings)) as reopened:
        restored = reopened.get(P + f"/conversations/{cid}", headers=admin).json()
        assert restored["attachment_ids"] == [aid]
        assert restored["attachments"][0]["id"] == aid
        assert restored["unavailable_attachment_ids"] == []
        second = ask(reopened, admin, cid, stream=True)
        assert "13.5" in second["answer"] and second["attachments"][0]["id"] == aid
        assert len(reopened.get(P + f"/conversations/{cid}/messages", headers=admin).json()) == 4


def test_patch_immediately_persists_order_rename_and_clear_without_deleting_files(client, admin):
    a = upload(client, admin, "a.txt").json()["id"]
    b = upload(client, admin, "b.txt").json()["id"]
    cid = create(client, admin)
    selected = change(client, admin, cid, [b, a])
    assert selected.status_code == 200 and selected.json()["attachment_ids"] == [b, a]
    assert [f["id"] for f in selected.json()["attachments"]] == [b, a]
    assert client.get(P + f"/conversations/{cid}/messages", headers=admin).json() == []
    client.patch(P + f"/attachments/{a}", headers=admin, json={"filename": "renamed.txt"}).raise_for_status()
    restored = client.get(P + f"/conversations/{cid}", headers=admin).json()
    assert restored["attachments"][1]["filename"] == "renamed.txt"
    assert change(client, admin, cid, []).json()["attachment_ids"] == []
    assert len(client.get(P + "/attachments", headers=admin).json()["items"]) == 2


def test_message_omission_and_null_reuse_but_empty_clears_selection(client, admin):
    aid = upload(client, admin).json()["id"]
    cid = create(client, admin)
    assert ask(client, admin, cid, attachment_ids=[aid])["attachments"][0]["id"] == aid
    assert ask(client, admin, cid, attachment_ids=None)["attachments"][0]["id"] == aid
    assert ask(client, admin, cid)["attachments"][0]["id"] == aid
    cleared = ask(client, admin, cid, attachment_ids=[])
    assert cleared["status"] == "general" and not cleared.get("attachments")
    assert client.get(P + f"/conversations/{cid}", headers=admin).json()["attachment_ids"] == []
    assert ask(client, admin, cid)["status"] == "general"


def test_changed_selection_resets_context_but_unchanged_keeps_followup_history(client, admin, monkeypatch):
    aid = upload(client, admin).json()["id"]
    cid = create(client, admin, [aid])
    ask(client, admin, cid)
    services = client.app.state.container
    original = services.orchestrator.run
    seen = []

    async def run(question, history, *args, **kwargs):
        seen.append(history)
        return await original(question, history, *args, **kwargs)

    monkeypatch.setattr(services.orchestrator, "run", run)
    assert change(client, admin, cid, [aid]).json()["scope_start_id"] == 0
    ask(client, admin, cid)
    assert len(seen[-1]) == 2
    assert change(client, admin, cid, []).json()["scope_start_id"] > 0
    ask(client, admin, cid)
    assert seen[-1] == []
    assert len(client.get(P + f"/conversations/{cid}/messages", headers=admin).json()) == 6


def test_deleted_selection_is_visible_as_unavailable_and_blocks_silent_fallback(client, admin, monkeypatch):
    a = upload(client, admin, "a.txt").json()["id"]
    b = upload(client, admin, "b.txt").json()["id"]
    cid = create(client, admin, [a, b])
    client.delete(P + f"/attachments/{a}", headers=admin).raise_for_status()
    restored = client.get(P + f"/conversations/{cid}", headers=admin).json()
    assert restored["attachment_ids"] == [a, b]  # Do not silently edit the user's persisted selection.
    assert restored["unavailable_attachment_ids"] == [a]
    assert [f["id"] for f in restored["attachments"]] == [b]
    services = client.app.state.container
    run = AsyncMock(side_effect=AssertionError("incomplete evidence must not generate"))
    monkeypatch.setattr(services.orchestrator, "run", run)
    result = client.post(P + f"/conversations/{cid}/messages", headers=admin, json={"question": "继续"})
    assert result.status_code == 404 and result.json()["error"]["code"] == "attachment_not_found"
    error = ask(client, admin, cid, stream=True)
    assert error["code"] == "attachment_not_found"
    run.assert_not_awaited()
    assert change(client, admin, cid, [b]).json()["unavailable_attachment_ids"] == []


def test_selection_owner_validation_limits_rollback_and_busy_lease(client, admin):
    aid = upload(client, admin).json()["id"]
    cid = create(client, admin, [aid])
    other = headers(register(client, "selection-other"))
    other_id = upload(client, other).json()["id"]
    assert client.get(P + f"/conversations/{cid}", headers=other).status_code == 404
    assert change(client, other, cid, []).status_code == 404
    assert change(client, admin, cid, [other_id]).status_code == 404
    assert change(client, admin, cid, [aid, aid]).status_code == 422
    assert client.get(P + f"/conversations/{cid}", headers=admin).json()["attachment_ids"] == [aid]
    services = client.app.state.container
    uid = services.repository.user(username="admin")["id"]
    token = services.repository.claim(cid, uid)
    try:
        assert change(client, admin, cid, []).status_code == 409
    finally:
        services.repository.release(cid, token)
    assert change(client, admin, cid, []).status_code == 200
    services.settings.attachment_max_chars = 1
    assert change(client, admin, cid, [aid]).status_code == 413
    assert client.get(P + f"/conversations/{cid}", headers=admin).json()["attachment_ids"] == []


def test_sessions_have_independent_selections_and_invalid_create_is_atomic(client, admin):
    a = upload(client, admin, "a.txt").json()["id"]
    b = upload(client, admin, "b.txt").json()["id"]
    first, second = create(client, admin, [a]), create(client, admin, [b])
    assert change(client, admin, first, []).status_code == 200
    assert client.get(P + f"/conversations/{second}", headers=admin).json()["attachment_ids"] == [b]
    response = client.post(
        P + "/conversations", headers=admin, json={"use_knowledge": False, "attachment_ids": ["0" * 32]}
    )
    assert response.status_code == 404
    assert len(client.get(P + "/conversations", headers=admin).json()) == 2


def test_migration_from_0007_preserves_existing_conversations_with_empty_selection(settings):
    database = Database(settings.database_url)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "app/database/migrations"))
    try:
        with database.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0007")
            connection.execute(
                text("INSERT INTO users (id,username,password_hash,role) VALUES ('u','legacy','hash','user')")
            )
            connection.execute(
                text(
                    "INSERT INTO conversations (id,user_id,title,created_at,lock_until) VALUES ('c','u','old',1,0)"
                )
            )
            command.upgrade(config, "head")
            row = connection.execute(text("SELECT title,attachment_ids FROM conversations")).one()
            assert row.title == "old" and json.loads(row.attachment_ids) == []
    finally:
        database.close()
