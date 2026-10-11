"""Offline SQLite security upgrade with encrypted, verified backup. Stop API workers first."""
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.common.encryption import Cipher, b64, unb64
from app.config import Settings
from app.infrastructure.container import Container


async def main():
    settings = Settings()
    if not settings.database_url.startswith("sqlite:///"):
        raise SystemExit("PostgreSQL: use pg_dump to an encrypted backup destination, then run alembic + app.cli init")
    cipher = Cipher(settings.encryption_key_file, create=True)
    database = Path(settings.database_url.removeprefix("sqlite:///"))
    with sqlite3.connect(database) as source, sqlite3.connect(":memory:") as copy:
        source.backup(copy)
        before = {table: copy.execute("SELECT count(*) FROM " + table).fetchone()[0]
                  for table in ("users", "conversations", "messages", "documents", "chunks")}
        raw = copy.serialize()
    backup_dir = ROOT / "data" / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / ("pre-security-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + ".rage-backup")
    backup.write_text(cipher.encrypt(b64(raw), "backup.sqlite"), encoding="utf-8")
    assert unb64(cipher.decrypt(backup.read_text(encoding="utf-8"), "backup.sqlite")) == raw
    services = Container(settings)
    try:
        await services.start()
        with sqlite3.connect(database) as connection:
            after = {table: connection.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in before}
        if before != after:
            raise RuntimeError("Record counts changed during migration; preserve backup and investigate")
        print(json.dumps({"backup": str(backup), "records_before": before, "records_after": after,
                          "encryption_key": str(cipher.path), "audit": services.security.verify_audit()}, ensure_ascii=False))
    finally:
        await services.close()


if __name__ == "__main__":
    asyncio.run(main())
