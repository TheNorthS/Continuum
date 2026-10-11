import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored, validate=True)
        actual = hashlib.scrypt(password.encode(), salt=raw[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(actual, raw[16:])
    except (ValueError, TypeError):
        return False


def issue_token(user_id: str, secret: str, minutes: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "exp": now + timedelta(minutes=minutes),
            "iss": "rag-e",
            "aud": "rag-e-ui",
        },
        secret,
        algorithm="HS256",
    )


def decode_token(token: str, secret: str) -> str:
    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience="rag-e-ui",
        issuer="rag-e",
        options={"require": ["sub", "exp", "iat"]},
    )["sub"]
