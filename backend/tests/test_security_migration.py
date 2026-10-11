import json
import os
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.database.session import Database
from app.infrastructure.container import Container


async def test_legacy_database_is_migrated_without_losing_history_or_documents(settings):
    db = Database(settings.database_url)
    cfg = Config("alembic.ini")
    with db.engine.begin() as c:
        cfg.attributes["connection"] = c
        command.upgrade(cfg, "0003")
        c.execute(
            text("INSERT INTO users (id,username,password_hash,role) VALUES ('old','olduser','hash','user')")
        )
        c.execute(
            text(
                "INSERT INTO conversations (id,user_id,title,created_at,lock_until) VALUES ('c','old','PRIVATE-TITLE',0,0)"
            )
        )
        c.execute(
            text(
                "INSERT INTO messages (conversation_id,role,content,details,created_at) VALUES ('c','user','PRIVATE-MESSAGE','{}',0)"
            )
        )
        c.execute(
            text(
                "INSERT INTO documents (id,filename,digest,status,chunk_count,created_at,graph_status) VALUES ('d','PRIVATE-FILE.md','digest','ready',1,0,'disabled')"
            )
        )
        c.execute(
            text(
                "INSERT INTO chunks (id,document_id,text,meta,embedding) VALUES ('d_0','d','PRIVATE-DOCUMENT',:meta,:embedding)"
            ),
            {
                "meta": json.dumps({"filename": "PRIVATE-FILE.md"}),
                "embedding": json.dumps([0.1] * settings.embedding_dimension),
            },
        )
    db.close()
    service = Container(settings)
    try:
        await service.start()
        assert service.repository.history("c", "old")[0]["content"] == "PRIVATE-MESSAGE"
        migrated = service.repository.conversation("c", "old")
        assert migrated["base_ids"] is None and migrated["scope_start_id"] == 0
        assert service.repository.documents()[0]["filename"] == "PRIVATE-FILE.md"
        assert service.repository.ready_chunks()[0]["text"] == "PRIVATE-DOCUMENT"
        with service.db.engine.connect() as c:
            assert c.execute(text("SELECT title FROM conversations")).scalar().startswith("rage1:")
            assert c.execute(text("SELECT text FROM chunks")).scalar().startswith("rage1:")
    finally:
        await service.close()
    path = settings.database_url.removeprefix("sqlite:///")
    assert b"PRIVATE-MESSAGE" not in open(path, "rb").read()


async def test_wrong_key_fails_closed_and_plaintext_is_not_silently_read(settings, sample, tmp_path):
    service = Container(settings)
    try:
        await service.start()
        await service.knowledge.ingest("secret.md", sample)
    finally:
        await service.close()
    settings.encryption_key_file = tmp_path / "unrelated.json"
    unrelated = Container(settings)
    try:
        with pytest.raises(ValueError, match="authenticated"):
            await unrelated.start()
    finally:
        await unrelated.close()


async def test_offline_rotation_command_preserves_data_and_audit(settings, sample):
    service = Container(settings)
    await service.start()
    await service.knowledge.ingest("rotation.md", sample)
    old = service.cipher.active
    await service.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAGE_")}
    env.update(
        RAGE_ENVIRONMENT="test",
        RAGE_MODE="demo",
        RAGE_DATABASE_URL=settings.database_url,
        RAGE_ENCRYPTION_KEY_FILE=str(settings.encryption_key_file),
        RAGE_GRAPH_ENABLED="false",
        RAGE_REDIS_URL="",
        PYTHONIOENCODING="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "keys-rotate", "--offline"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    reopened = Container(settings)
    try:
        await reopened.start()
        assert reopened.cipher.active != old
        assert reopened.repository.documents()[0]["filename"] == "rotation.md"
        assert reopened.security.verify_audit()["valid"]
    finally:
        await reopened.close()
