"""Versioned AES-256-GCM keyring. Ciphertext is bound to its field/cache namespace."""

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text, TypeDecorator, text


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class Cipher:
    def __init__(self, path: Path, create=False):
        self.path = path
        if not path.exists() and create:
            path.parent.mkdir(parents=True, exist_ok=True)
            value = {
                "active": "k1",
                "keys": {"k1": b64(secrets.token_bytes(32))},
                "index_key": b64(secrets.token_bytes(32)),
                "audit_key": b64(secrets.token_bytes(32)),
            }
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(value, f)
            except FileExistsError:
                pass
        self.reload()

    def reload(self):
        self.ring = json.loads(self.path.read_text(encoding="utf-8"))
        self.active = self.ring["active"]
        self.keys = {kid: unb64(key) for kid, key in self.ring["keys"].items()}
        if self.active not in self.keys or any(len(key) != 32 for key in self.keys.values()):
            raise ValueError("Invalid encryption keyring")
        self.index_key = unb64(self.ring["index_key"])
        self.audit_key = unb64(self.ring["audit_key"])
        if len(self.index_key) != 32 or len(self.audit_key) != 32:
            raise ValueError("Invalid index/audit key")

    def encrypt(self, value, aad):
        nonce = secrets.token_bytes(12)
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        payload = AESGCM(self.keys[self.active]).encrypt(nonce, raw, aad.encode())
        return f"rage1:{self.active}:{b64(nonce + payload)}"

    def decrypt(self, value, aad):
        if isinstance(value, bytes):
            value = value.decode()
        try:
            version, kid, payload = value.split(":", 2)
            if version != "rage1":
                raise ValueError("Unencrypted value")
            raw = unb64(payload)
            return json.loads(AESGCM(self.keys[kid]).decrypt(raw[:12], raw[12:], aad.encode()))
        except Exception as exc:
            raise ValueError("Encrypted data cannot be authenticated; check keyring") from exc

    def blind(self, value, purpose="index"):
        return hmac.new(self.index_key, (purpose + ":" + value).encode(), hashlib.sha256).hexdigest()

    def sign_audit(self, value):
        return hmac.new(self.audit_key, value.encode(), hashlib.sha256).hexdigest()

    def rotate(self):
        kid = "k" + secrets.token_hex(6)
        self.ring["keys"][kid] = b64(secrets.token_bytes(32))
        self.ring["active"] = kid
        temporary = self.path.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.ring, f)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "nt":
            # Preserve the destination NTFS DACL during replacement (os.replace does not merge ACLs).
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            replace = kernel.ReplaceFileW
            replace.argtypes = [
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.LPVOID,
            ]
            replace.restype = wintypes.BOOL
            if not replace(str(self.path.resolve()), str(temporary.resolve()), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporary, self.path)
        self.reload()
        return kid


class Encrypted(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, purpose):
        self.purpose = purpose
        super().__init__()

    def process_bind_param(self, value, dialect):
        return None if value is None else dialect.rage_cipher.encrypt(value, self.purpose)

    def process_result_value(self, value, dialect):
        return None if value is None else dialect.rage_cipher.decrypt(value, self.purpose)

    def copy(self, **kwargs):
        return Encrypted(self.purpose)


FIELDS = {
    "memory_events": {"content": True},
    "memory_artifacts": {"payload": True},
    "compaction_checkpoints": {"summary": True},
    "memory_records": {"payload": True, "embedding": True},
    "memory_candidates": {"payload": True},
    "attachments": {"filename": False, "payload": True},
    "conversations": {"title": False},
    "messages": {"content": False, "details": True},
    "documents": {"filename": False, "error": False, "graph_error": False},
    "chunks": {"text": False, "meta": True, "embedding": True},
    "knowledge_bases": {"name": False},
}


def encrypt_database(db, *, allow_plaintext=False):
    """Maintenance only. New ORM reads never silently accept plaintext."""
    count = 0
    with db.engine.begin() as connection:
        for table, fields in FIELDS.items():
            # Table/column names come exclusively from this internal constant.
            rows = connection.execute(text(f"SELECT id, {','.join(fields)} FROM {table}")).mappings()
            for row in rows:
                changes = {}
                for column, is_json in fields.items():
                    value = row[column]
                    if value is None:
                        continue
                    purpose = f"{table}.{column}"
                    if isinstance(value, str) and value.startswith("rage1:"):
                        decoded = db.cipher.decrypt(value, purpose)
                        if value.split(":")[1] == db.cipher.active:
                            continue
                    else:
                        if not allow_plaintext:
                            raise ValueError("Plaintext encountered outside approved migration")
                        decoded = json.loads(value) if is_json and isinstance(value, str) else value
                    changes[column] = db.cipher.encrypt(decoded, purpose)
                if changes:
                    connection.execute(
                        text(
                            f"UPDATE {table} SET "
                            + ",".join(f"{c}=:{c}" for c in changes)
                            + " WHERE id=:row_id"
                        ),
                        {**changes, "row_id": row["id"]},
                    )
                    count += 1
    return count
