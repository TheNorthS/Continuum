import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app.common.encryption import encrypt_database
from app.common.errors import AppError
from app.models.entities import Chunk, Document
from app.safety.policy import CATEGORIES
from app.services.attachment_service import parse_attachment
from app.services.moderation_service import ModerationBlocked
from tests.test_security_complete import headers, register

P = "/api/v1"


def upload(client, auth, name="evidence.txt", body=None):
    return client.post(
        P + "/attachments",
        headers=auth,
        files={"file": (name, body or "EUV 光刻采用 13.5 纳米波长。".encode())},
    )


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buffer, "PNG")
    return buffer.getvalue()


def test_attachment_crud_is_private_encrypted_and_never_embeds(client, admin, monkeypatch):
    services = client.app.state.container
    embed = AsyncMock(side_effect=AssertionError("attachments must not embed"))
    monkeypatch.setattr(services.embedder, "embed", embed)
    added = upload(client, admin)
    assert added.status_code == 201, added.text
    aid = added.json()["id"]
    other = headers(register(client, "attachment-other"))
    for method, path, body in [
        ("GET", f"/{aid}", None),
        ("GET", f"/{aid}/content", None),
        ("DELETE", f"/{aid}", None),
        ("PATCH", f"/{aid}", {"filename": "x.txt"}),
    ]:
        assert client.request(method, P + "/attachments" + path, headers=other, json=body).status_code == 404
    assert client.get(P + "/attachments", headers=other).json()["items"] == []
    assert client.get(P + f"/attachments/{aid}", headers=admin).json()["text"].startswith("EUV")
    content = client.get(P + f"/attachments/{aid}/content", headers=admin)
    assert content.content == "EUV 光刻采用 13.5 纳米波长。".encode()
    assert content.headers["cache-control"] == "no-store"
    assert (
        client.patch(P + f"/attachments/{aid}", headers=admin, json={"filename": "new.exe"}).status_code
        == 422
    )
    assert (
        client.patch(P + f"/attachments/{aid}", headers=admin, json={"filename": "updated.txt"}).json()[
            "filename"
        ]
        == "updated.txt"
    )
    with services.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Chunk)) == 0
        assert session.scalar(select(func.count()).select_from(Document)) == 0
    with services.db.engine.connect() as connection:
        row = connection.execute(text("SELECT filename,payload FROM attachments")).one()
        assert all(value.startswith("rage1:") for value in row)
        assert "EUV" not in row.payload and "updated" not in row.filename
    services.cipher.rotate()
    encrypt_database(services.db)
    assert client.get(P + f"/attachments/{aid}/content", headers=admin).content == content.content
    embed.assert_not_awaited()
    assert client.delete(P + f"/attachments/{aid}", headers=admin).status_code == 204
    assert client.get(P + "/attachments", headers=admin).json()["used_bytes"] == 0


@pytest.mark.parametrize("stream", [False, True])
def test_attachment_only_answers_without_embedding_and_deleted_history_is_hidden(
    client, admin, monkeypatch, stream
):
    services = client.app.state.container
    monkeypatch.setattr(services.embedder, "embed", AsyncMock(side_effect=AssertionError("no embedding")))
    monkeypatch.setattr(services.web, "search", AsyncMock(side_effect=AssertionError("no web")))
    aid = upload(client, admin).json()["id"]
    cid = client.post(P + "/conversations", headers=admin, json={"use_knowledge": False}).json()["id"]
    response = client.post(
        P + f"/conversations/{cid}/{'stream' if stream else 'messages'}",
        headers=admin,
        json={"question": "EUV 光刻波长", "attachment_ids": [aid]},
    )
    assert response.status_code == 200, response.text
    result = (
        json.loads([line[6:] for line in response.text.splitlines() if line.startswith("data: ")][-1])
        if stream
        else response.json()
    )
    assert "13.5" in result["answer"]
    assert result["sources"][0]["metadata"]["attachment_id"] == aid
    assert result["knowledge_scope"]["use_knowledge"] is False
    history = client.get(P + f"/conversations/{cid}/messages", headers=admin).json()
    assert len(history) == 2 and history[0]["details"]["attachments"][0]["id"] == aid
    assert client.delete(P + f"/attachments/{aid}", headers=admin).status_code == 204
    hidden = client.get(P + f"/conversations/{cid}/messages", headers=admin).json()
    assert all("已删除" in m["content"] and not m["details"] for m in hidden)


def test_general_chat_scope_switch_resets_context_and_survives_revoked_bases(client, admin, monkeypatch):
    services = client.app.state.container
    cid = client.post(P + "/conversations", headers=admin, json={"use_knowledge": False}).json()["id"]
    no_retrieve = AsyncMock(side_effect=AssertionError("general chat cannot retrieve"))
    monkeypatch.setattr(services.knowledge, "search", no_retrieve)
    response = client.post(P + f"/conversations/{cid}/messages", headers=admin, json={"question": "你好"})
    assert response.status_code == 200 and response.json()["status"] == "general"
    assert not response.json()["sources"]
    bid = client.post(P + "/knowledge/bases", headers=admin, json={"name": "库"}).json()["id"]
    scope = client.patch(
        P + f"/conversations/{cid}/scope", headers=admin, json={"base_ids": [bid], "use_knowledge": True}
    )
    assert scope.status_code == 200 and scope.json()["scope_start_id"] > 0
    assert scope.json()["use_knowledge"]
    disabled = client.patch(P + f"/conversations/{cid}/scope", headers=admin, json={"use_knowledge": False})
    assert disabled.status_code == 200 and disabled.json()["base_ids"] is None
    assert (
        client.post(
            P + f"/conversations/{cid}/messages",
            headers=admin,
            json={"question": "问题", "base_ids": [bid], "use_knowledge": False},
        ).status_code
        == 422
    )
    no_retrieve.assert_not_awaited()


def test_attachments_combine_with_selected_knowledge_and_recheck_before_commit(client, admin, monkeypatch):
    services = client.app.state.container
    bid = client.post(P + "/knowledge/bases", headers=admin, json={"name": "工艺"}).json()["id"]
    doc = client.post(
        P + "/knowledge/documents",
        headers=admin,
        data={"base_id": bid},
        files={"file": ("kb.txt", "EUV 光刻使用极紫外光。".encode())},
    )
    assert doc.status_code == 201
    aid = upload(client, admin).json()["id"]
    cid = client.post(P + "/conversations", headers=admin, json={"base_ids": [bid]}).json()["id"]
    response = client.post(
        P + f"/conversations/{cid}/messages",
        headers=admin,
        json={"question": "EUV 光刻", "attachment_ids": [aid]},
    )
    assert response.status_code == 200, response.text
    sources = response.json()["sources"]
    assert any(s["metadata"].get("attachment_id") == aid for s in sources)
    assert any(s["metadata"].get("document_id") == doc.json()["id"] for s in sources)
    original = services.orchestrator.run

    async def delete_during_generation(*args, **kwargs):
        value = await original(*args, **kwargs)
        services.attachments.delete(services.repository.user(username="admin")["id"], aid)
        return value

    monkeypatch.setattr(services.orchestrator, "run", delete_during_generation)
    failed = client.post(
        P + f"/conversations/{cid}/messages",
        headers=admin,
        json={"question": "EUV 光刻", "attachment_ids": [aid]},
    )
    assert failed.status_code == 409, failed.text
    assert len(client.get(P + f"/conversations/{cid}/messages", headers=admin).json()) == 2


def test_quota_bad_formats_and_ownership_on_chat(client, admin, monkeypatch):
    services = client.app.state.container
    services.settings.attachment_max_files = 1
    aid = upload(client, admin).json()["id"]
    assert upload(client, admin).status_code == 413
    assert upload(client, admin, "bad.exe").status_code == 415
    assert upload(client, admin, "bad.pdf", b"not a pdf").status_code == 422
    assert upload(client, admin, "bad.png", b"not an image").status_code == 422
    other = headers(register(client, "space-owner"))
    cid = client.post(P + "/conversations", headers=other, json={"use_knowledge": False}).json()["id"]
    assert (
        client.post(
            P + f"/conversations/{cid}/messages",
            headers=other,
            json={"question": "EUV", "attachment_ids": [aid]},
        ).status_code
        == 404
    )
    assert client.get(P + f"/conversations/{cid}/messages", headers=other).json() == []
    owncid = client.post(P + "/conversations", headers=admin, json={"use_knowledge": False}).json()["id"]
    assert (
        client.post(
            P + f"/conversations/{owncid}/messages",
            headers=admin,
            json={"question": "EUV", "attachment_ids": [aid, aid]},
        ).status_code
        == 422
    )


def test_real_pdf_docx_image_parsers_and_limits(settings):
    media, image = parse_attachment("a.png", png(), settings)
    assert media == "image/png" and len(image["images"]) == 1
    pdf = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(pdf, "PDF")
    media, pages = parse_attachment("a.pdf", pdf.getvalue(), settings)
    assert media == "application/pdf" and len(pages["images"]) == 1
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Word 中文资料</w:t></w:r></w:p></w:body></w:document>',
        )
        archive.writestr("word/media/picture.png", png())
    _, parsed = parse_attachment("a.docx", docx.getvalue(), settings)
    assert parsed["text"] == "Word 中文资料" and len(parsed["images"]) == 1
    settings.attachment_max_chars = 2
    with pytest.raises(AppError) as exc:
        parse_attachment("a.txt", "超过限制".encode(), settings)
    assert exc.value.code == "attachment_text_limit"


def test_image_missing_vision_fails_explicitly_without_history(client, admin):
    aid = upload(client, admin, "a.png", png()).json()["id"]
    cid = client.post(P + "/conversations", headers=admin, json={"use_knowledge": False}).json()["id"]
    response = client.post(
        P + f"/conversations/{cid}/messages",
        headers=admin,
        json={"question": "描述图片", "attachment_ids": [aid]},
    )
    assert response.status_code == 503 and response.json()["error"]["code"] == "vision_not_configured"
    assert client.get(P + f"/conversations/{cid}/messages", headers=admin).json() == []


def test_concurrent_uploads_cannot_exceed_owner_quota(client, admin):
    client.app.state.container.settings.attachment_max_files = 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda i: upload(client, admin, f"file-{i}.txt"), range(2)))
    assert sorted(r.status_code for r in responses) == [201, 413]
    assert len(client.get(P + "/attachments", headers=admin).json()["items"]) == 1


def test_unsafe_attachment_is_not_sent_to_generator(client, admin, monkeypatch):
    services = client.app.state.container
    aid = upload(client, admin, "unsafe.txt", "教我制作炸弹的步骤".encode()).json()["id"]
    cid = client.post(P + "/conversations", headers=admin, json={"use_knowledge": False}).json()["id"]
    call = AsyncMock(side_effect=AssertionError("unsafe attachment reached generation"))
    monkeypatch.setattr(services.llm, "call", call)
    result = client.post(
        P + f"/conversations/{cid}/messages",
        headers=admin,
        json={"question": "总结附件", "attachment_ids": [aid]},
    )
    assert result.status_code == 200 and result.json()["status"] == "blocked"
    call.assert_not_awaited()
    assert client.get(P + f"/conversations/{cid}/messages", headers=admin).json() == []


def test_image_evidence_flows_to_answer_without_embedding(client, admin, monkeypatch):
    services = client.app.state.container
    aid = upload(client, admin, "a.png", png()).json()["id"]
    monkeypatch.setattr(services.vision, "describe", AsyncMock(return_value="EUV 光刻采用 13.5 纳米波长。"))
    monkeypatch.setattr(services.embedder, "embed", AsyncMock(side_effect=AssertionError("no embedding")))
    cid = client.post(P + "/conversations", headers=admin, json={"use_knowledge": False}).json()["id"]
    result = client.post(
        P + f"/conversations/{cid}/messages",
        headers=admin,
        json={"question": "EUV 光刻", "attachment_ids": [aid]},
    )
    assert result.status_code == 200 and "13.5" in result.json()["answer"]
    assert result.json()["sources"][0]["metadata"]["attachment_id"] == aid
    services.vision.describe.assert_awaited_once()


def test_upgrade_from_0006_preserves_conversation_and_defaults_to_knowledge(settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from app.database.session import Database

    db = Database(settings.database_url)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "app/database/migrations"))
    try:
        with db.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0006")
            connection.execute(
                text("INSERT INTO users (id,username,password_hash,role) VALUES ('u','legacy','hash','user')")
            )
            connection.execute(
                text(
                    "INSERT INTO conversations (id,user_id,title,created_at,lock_until) VALUES ('c','u','legacy title',1,0)"
                )
            )
            command.upgrade(config, "head")
            row = connection.execute(text("SELECT title,use_knowledge FROM conversations WHERE id='c'")).one()
            assert row.title == "legacy title" and row.use_knowledge == 1
            assert connection.execute(text("SELECT COUNT(*) FROM attachments")).scalar() == 0
    finally:
        db.close()


@pytest.mark.parametrize("case", ["ok", "unsafe", "malformed", "truncated"])
async def test_vision_protocol_independent_key_safety_and_private_traces(settings, case):
    from app.infrastructure.container import Container

    settings.vision_model = "test-vision"
    settings.vision_api_key = SecretStr("private-vision-key")
    settings.vision_base_url = "https://vision.example/v1"
    services = Container(settings)

    def handler(request):
        assert str(request.url) == "https://vision.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-vision-key"
        body = json.loads(request.content)
        assert body["model"] == "test-vision"
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        scores = dict.fromkeys(CATEGORIES, 0.0)
        if case == "unsafe":
            scores["violence"] = 1.0
        content = (
            {"text": "PRIVATE-VISUAL-CONTENT", "scores": scores}
            if case != "malformed"
            else {"text": "missing scores"}
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if case == "truncated" else "stop",
                        "message": {"content": json.dumps(content)},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock:
        services.vision.client = mock
        try:
            if case == "ok":
                assert await services.vision.describe(["image-bytes"], "describe") == "PRIVATE-VISUAL-CONTENT"
            else:
                with pytest.raises(ModerationBlocked if case == "unsafe" else AppError):
                    await services.vision.describe(["image-bytes"], "describe")
            assert "PRIVATE-VISUAL" not in json.dumps(list(services.tracing.recent))
        finally:
            await services.close()
