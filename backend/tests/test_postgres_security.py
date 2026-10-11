import os
import uuid

import pytest

from app.main import create_app
from tests.signed_client import SignedClient
from tests.test_security_complete import (
    test_encryption_audit_tamper_redaction_and_rotation as check_encryption,
)
from tests.test_security_complete import test_nonce_unique_across_concurrent_requests as check_nonce


@pytest.mark.external
@pytest.mark.skipif(not os.getenv("RAGE_TEST_POSTGRES_URL"), reason="test PostgreSQL not configured")
def test_postgres_encryption_nonce_and_audit_transactions(settings):
    import psycopg
    from psycopg import sql

    url = os.environ["RAGE_TEST_POSTGRES_URL"]
    name = "rage_security_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings.database_url = (
        url.replace("postgresql://", "postgresql+psycopg://").rsplit("/", 1)[0] + "/" + name
    )
    try:
        with SignedClient(create_app(settings)) as client:
            data = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "rag-e-demo-2026"}
            ).json()
            admin = {"Authorization": "Bearer " + data["access_token"]}
            check_nonce(client, admin)
            check_encryption(client, admin)
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
