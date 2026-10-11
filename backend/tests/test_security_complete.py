import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.common.context import actor
from app.common.encryption import encrypt_database
from app.common.redaction import redact
from tests.signed_client import KEYS, proof_for

P = "/api/v1"


def register(client, name):
    response = client.post(P + "/auth/register", json={"username": name, "password": name + "-password-123"})
    assert response.status_code == 201, response.text
    return client.post(P + "/auth/login", json={"username": name, "password": name + "-password-123"}).json()


def headers(data):
    return {"Authorization": "Bearer " + data["access_token"]}


def test_replay_exact_bytes_method_path_and_stolen_token(client, admin):
    token = admin["Authorization"].split()[1]
    key = KEYS[token]
    req = client.build_request("POST", P + "/conversations", headers=admin, json={"title": "safe"})
    req.headers["X-Request-Proof"] = proof_for(req, key, token)
    assert client.send(req).status_code == 201
    assert client.send(req).status_code == 409
    wrong = client.build_request("POST", P + "/conversations", headers=admin, json={"title": "tampered"})
    wrong.headers["X-Request-Proof"] = req.headers["X-Request-Proof"]
    assert client.send(wrong).status_code == 401
    for target in (P + "/system", P + "/knowledge/documents?extra=true"):
        changed = client.build_request("GET", target, headers=admin)
        changed.headers["X-Request-Proof"] = req.headers["X-Request-Proof"]
        assert client.send(changed).status_code == 401
    raw = TestClient(client.app)
    assert raw.get(P + "/conversations", headers=admin).status_code == 401
    forged = client.build_request("GET", P + "/conversations", headers=admin)
    forged.headers["X-Request-Proof"] = proof_for(forged, ec.generate_private_key(ec.SECP256R1()), token)
    assert client.send(forged).status_code == 401
    forged.headers["X-Request-Proof"] = proof_for(forged, key, token, issued=time.time() - 120)
    assert client.send(forged).status_code == 401


def test_nonce_unique_across_concurrent_requests(client, admin):
    token = admin["Authorization"].split()[1]
    nonce = uuid.uuid4().hex

    def send(_):
        req = client.build_request("GET", P + "/system", headers=admin)
        req.headers["X-Request-Proof"] = proof_for(req, KEYS[token], token, jti=nonce)
        return client.send(req).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(send, range(4)))
    assert sorted(outcomes) == [200, 409, 409, 409]


def test_refresh_rotation_reuse_revokes_session_and_logout(client):
    data = register(client, "refreshuser")
    next_tokens = client.post(P + "/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert next_tokens.status_code == 200, next_tokens.text
    next_tokens = next_tokens.json()
    reused = client.post(P + "/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert reused.status_code == 401
    assert client.get(P + "/system", headers=headers(next_tokens)).status_code == 401
    fresh = client.post(
        P + "/auth/login", json={"username": "refreshuser", "password": "refreshuser-password-123"}
    ).json()
    assert client.post(P + "/auth/logout", headers=headers(fresh)).status_code == 204
    assert client.get(P + "/system", headers=headers(fresh)).status_code == 401


def test_accounts_custom_roles_revocation_last_admin_and_password_reset(client, admin):
    data = register(client, "member")
    uid, auth = data["user"]["id"], headers(data)
    assert client.get(P + "/admin/users", headers=auth).status_code == 403
    role = {"id": "reader", "permissions": ["chat.use", "knowledge.read", "graph.read"]}
    assert client.post(P + "/admin/roles", headers=admin, json=role).status_code == 204
    assert client.patch(P + "/admin/users/" + uid, headers=admin, json={"role": "reader"}).status_code == 200
    assert client.get(P + "/system", headers=auth).status_code == 401
    data = client.post(
        P + "/auth/login", json={"username": "member", "password": "member-password-123"}
    ).json()
    auth = headers(data)
    assert client.post(P + "/knowledge/bases", headers=auth, json={"name": "denied"}).status_code == 403
    assert client.delete(P + "/admin/roles/reader", headers=admin).status_code == 409
    aid = client.get(P + "/auth/me", headers=admin).json()["id"]
    assert client.patch(P + "/admin/users/" + aid, headers=admin, json={"active": False}).status_code == 409
    assert (
        client.patch(
            P + "/admin/users/" + uid, headers=admin, json={"password": "temporary-password-123"}
        ).status_code
        == 200
    )
    assert client.get(P + "/system", headers=auth).status_code == 401
    data = client.post(
        P + "/auth/login", json={"username": "member", "password": "temporary-password-123"}
    ).json()
    assert data["user"]["must_change_password"]
    assert client.get(P + "/knowledge/documents", headers=headers(data)).status_code == 403
    assert (
        client.post(
            P + "/auth/password",
            headers=headers(data),
            json={"old_password": "temporary-password-123", "new_password": "member-new-password-123"},
        ).status_code
        == 204
    )
    assert client.get(P + "/auth/me", headers=headers(data)).status_code == 401
    assert client.patch(P + "/admin/users/" + uid, headers=admin, json={"active": False}).status_code == 200
    assert (
        client.post(
            P + "/auth/login", json={"username": "member", "password": "member-new-password-123"}
        ).status_code
        == 401
    )


def test_resource_acl_cache_partition_revoke_and_private_conversations(client, admin):
    alice, bob = register(client, "aliceprivate"), register(client, "bobprivate")
    a, b = headers(alice), headers(bob)
    base = client.post(P + "/knowledge/bases", headers=a, json={"name": "Alice private"}).json()
    response = client.post(
        P + "/knowledge/documents",
        headers=a,
        data={"base_id": base["id"]},
        files={"file": ("secret.md", b"# Confidential\nORCHID-SECRET project uses a private protocol.")},
    )
    assert response.status_code == 201, response.text
    doc = response.json()
    query = {"query": "ORCHID-SECRET", "top_k": 4}
    assert client.post(P + "/knowledge/search", headers=a, json=query).json()["results"]
    assert client.post(P + "/knowledge/search", headers=b, json=query).json()["results"] == []
    assert client.get(P + "/knowledge/documents", headers=b).json() == []
    assert client.delete(P + "/knowledge/documents/" + doc["id"], headers=b).status_code == 404
    grant = {"subject_type": "user", "subject_id": bob["user"]["id"], "access": "read"}
    assert client.put(f"{P}/knowledge/bases/{base['id']}/grants", headers=a, json=grant).status_code == 204
    assert client.post(P + "/knowledge/search", headers=b, json=query).json()["results"]
    assert client.delete(P + "/knowledge/documents/" + doc["id"], headers=b).status_code == 404
    grant["access"] = None
    assert client.put(f"{P}/knowledge/bases/{base['id']}/grants", headers=a, json=grant).status_code == 204
    assert client.post(P + "/knowledge/search", headers=b, json=query).json()["results"] == []
    cid = client.post(P + "/conversations", headers=a, json={}).json()["id"]
    assert client.get(f"{P}/conversations/{cid}/messages", headers=b).status_code == 404
    assert client.get(f"{P}/conversations/{cid}/messages", headers=admin).status_code == 404


def test_encryption_audit_tamper_redaction_and_rotation(client, admin):
    secret = "ORCHID-SECRET-123"
    response = client.post(
        P + "/knowledge/documents",
        headers=admin,
        files={"file": ("confidential.md", ("# Private\n" + secret).encode())},
    )
    assert response.status_code == 201
    services = client.app.state.container
    with services.db.engine.connect() as c:
        stored = c.execute(text("SELECT text,meta,embedding FROM chunks")).first()
        assert all(value.startswith("rage1:") for value in stored)
        assert secret not in repr(stored) and "confidential.md" not in repr(stored)
    token = actor.set(None)
    try:
        services.security.audit(
            "test.redact",
            details={
                "password": secret,
                "message": "mail user@example.com phone 13812345678",
                "authorization": admin["Authorization"],
            },
        )
        assert services.security.verify_audit()["valid"]
        events = services.security.audit_list(limit=200)
        assert secret not in json.dumps(events) and "user@example.com" not in json.dumps(events)
        with services.db.engine.begin() as c:
            c.execute(text("UPDATE audit_events SET outcome='tampered' WHERE id=1"))
        assert not services.security.verify_audit()["valid"]
        # Restore by rollback is intentionally not provided: audit tampering remains detectable.
        old = services.cipher.encrypt({"secret": secret}, "test")
        old_id = services.cipher.active
        services.cipher.rotate()
        assert old_id != services.cipher.active and services.cipher.decrypt(old, "test")["secret"] == secret
        with pytest.raises(ValueError):
            services.cipher.decrypt(old, "other-field")
        encrypt_database(services.db)
        assert services.repository.ready_chunks()[0]["text"].endswith(secret)
    finally:
        actor.reset(token)


def test_redaction_recursive():
    result = redact(
        {
            "authorization": "secret",
            "data": [{"api_key": "sk-test"}],
            "message": "password=hello123 email=a@b.com",
        }
    )
    assert result["authorization"] == "[REDACTED]"
    assert result["data"][0]["api_key"] == "[REDACTED]"
    assert "hello123" not in result["message"] and "a@b.com" not in result["message"]


def test_production_https_cannot_be_bypassed_by_untrusted_forwarded_header(settings):
    from app.main import create_app
    from tests.signed_client import SignedClient

    # Configure validated production-equivalent transport on the test fixture; no external models are called.
    settings.environment = "production"
    from pydantic import SecretStr

    settings.admin_password = SecretStr("production-test-password-123")
    from app.common.encryption import Cipher

    Cipher(settings.encryption_key_file, create=True)
    with SignedClient(create_app(settings)) as client:
        assert client.get(P + "/health/live").status_code == 200
        assert client.get(P + "/auth/challenge").status_code == 400
        assert client.get(P + "/auth/challenge", headers={"X-Forwarded-Proto": "https"}).status_code == 400
    with SignedClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get(P + "/auth/challenge").status_code == 200
        assert client.get("/docs").status_code == 404


def test_role_escalation_is_rejected(client, admin):
    member = register(client, "limitedmanager")
    uid = member["user"]["id"]
    role = {"id": "usermgr", "permissions": ["users.manage", "roles.manage"]}
    client.post(P + "/admin/roles", headers=admin, json=role)
    client.patch(P + "/admin/users/" + uid, headers=admin, json={"role": "usermgr"})
    signed = client.post(
        P + "/auth/login", json={"username": "limitedmanager", "password": "limitedmanager-password-123"}
    ).json()
    limited = headers(signed)
    assert (
        client.post(
            P + "/admin/users",
            headers=limited,
            json={"username": "badadmin", "password": "verylongpassword", "role": "admin"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            P + "/admin/roles", headers=limited, json={"id": "super", "permissions": ["audit.read"]}
        ).status_code
        == 403
    )
    assert client.patch(P + "/admin/users/" + uid, headers=limited, json={"role": "admin"}).status_code == 403


def test_historical_answer_is_hidden_after_access_revocation(client):
    alice, bob = register(client, "histalice"), register(client, "histbob")
    a, b = headers(alice), headers(bob)
    base = client.post(P + "/knowledge/bases", headers=a, json={"name": "History private"}).json()
    client.post(
        P + "/knowledge/documents",
        headers=a,
        data={"base_id": base["id"]},
        files={"file": ("history.md", b"# Secret\nHISTORY-SECRET protocol description")},
    )
    grant = {"subject_type": "user", "subject_id": bob["user"]["id"], "access": "read"}
    client.put(f"{P}/knowledge/bases/{base['id']}/grants", headers=a, json=grant)
    cid = client.post(P + "/conversations", headers=b, json={}).json()["id"]
    answer = client.post(f"{P}/conversations/{cid}/messages", headers=b, json={"question": "HISTORY-SECRET"})
    assert answer.status_code == 200 and "HISTORY-SECRET" in answer.json()["answer"]
    grant["access"] = None
    client.put(f"{P}/knowledge/bases/{base['id']}/grants", headers=a, json=grant)
    messages = client.get(f"{P}/conversations/{cid}/messages", headers=b).json()
    assert messages[-1]["content"] == "该回答引用的部分知识库权限已撤销，内容已隐藏。"
    assert messages[-1]["details"] == {}
