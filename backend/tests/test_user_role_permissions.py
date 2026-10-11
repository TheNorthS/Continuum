from app.main import create_app
from tests.signed_client import SignedClient
from tests.test_security_complete import P, headers, register


def test_admin_edits_user_role_permissions_immediately_and_persists(client, admin, settings):
    member = register(client, "role-member")
    auth = headers(member)
    assert client.post(P + "/knowledge/bases", headers=auth, json={"name": "before"}).status_code == 201
    permissions = ["chat.use", "knowledge.read"]
    saved = client.post(P + "/admin/roles", headers=admin, json={"id": "user", "permissions": permissions})
    assert saved.status_code == 204, saved.text
    # An already-issued token sees the new permissions without requiring logout.
    assert client.get(P + "/auth/me", headers=auth).json()["permissions"] == permissions
    assert client.post(P + "/knowledge/bases", headers=auth, json={"name": "denied"}).status_code == 403
    role = next(r for r in client.get(P + "/admin/roles", headers=admin).json()["roles"] if r["id"] == "user")
    assert role["builtin"] is True
    assert client.delete(P + "/admin/roles/user", headers=admin).status_code == 409
    assert (
        client.post(P + "/admin/roles", headers=admin, json={"id": "admin", "permissions": []}).status_code
        == 409
    )
    with SignedClient(create_app(settings)) as reopened:
        assert reopened.get(P + "/auth/me", headers=auth).json()["permissions"] == permissions
        new = register(reopened, "role-new-member")
        assert new["user"]["permissions"] == permissions
    events = client.get(P + "/admin/audit", headers=admin).json()
    assert any(e["action"] == "role.save" and e["resource"] == "user" for e in events)
    assert client.get(P + "/admin/audit/verify", headers=admin).json()["valid"]


def test_delegated_role_manager_cannot_edit_user_or_higher_privilege_roles(client, admin):
    manager = register(client, "delegated-manager")
    assert (
        client.post(
            P + "/admin/roles", headers=admin, json={"id": "manager", "permissions": ["roles.manage"]}
        ).status_code
        == 204
    )
    assert (
        client.patch(
            P + "/admin/users/" + manager["user"]["id"], headers=admin, json={"role": "manager"}
        ).status_code
        == 200
    )
    login = client.post(
        P + "/auth/login",
        json={"username": "delegated-manager", "password": "delegated-manager-password-123"},
    ).json()
    auth = headers(login)
    assert (
        client.post(P + "/admin/roles", headers=auth, json={"id": "user", "permissions": []}).status_code
        == 403
    )
    assert (
        client.post(
            P + "/admin/roles", headers=admin, json={"id": "powerful", "permissions": ["users.manage"]}
        ).status_code
        == 204
    )
    assert (
        client.post(P + "/admin/roles", headers=auth, json={"id": "powerful", "permissions": []}).status_code
        == 403
    )
    assert (
        client.post(P + "/admin/roles", headers=auth, json={"id": "safe", "permissions": []}).status_code
        == 204
    )


def test_unknown_permissions_do_not_change_user_role(client, admin):
    before = client.get(P + "/admin/roles", headers=admin).json()
    assert (
        client.post(
            P + "/admin/roles", headers=admin, json={"id": "user", "permissions": ["invalid.permission"]}
        ).status_code
        == 422
    )
    assert client.get(P + "/admin/roles", headers=admin).json() == before
