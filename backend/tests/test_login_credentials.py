from sqlalchemy import select

from app.models.entities import User
from app.utils.security import hash_password


def test_existing_short_password_login_and_upgrade_preserve_creation_policy(client):
    with client.app.state.container.db.session.begin() as session:
        user = session.scalar(select(User).where(User.username == "admin"))
        user.password_hash = hash_password("admin")
    prefix = "/api/v1/auth"
    response = client.post(prefix + "/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200, response.text
    auth = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert client.get(prefix + "/me", headers=auth).status_code == 200
    assert client.post(prefix + "/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post(prefix + "/login", json={"username": "admin", "password": ""}).status_code == 422
    assert (
        client.post(prefix + "/register", json={"username": "shortuser", "password": "admin"}).status_code
        == 422
    )
    assert (
        client.post(
            prefix + "/password", headers=auth, json={"old_password": "admin", "new_password": "short"}
        ).status_code
        == 422
    )
    changed = client.post(
        prefix + "/password",
        headers=auth,
        json={"old_password": "admin", "new_password": "upgraded-password-123"},
    )
    assert changed.status_code == 204, changed.text
    assert client.get(prefix + "/me", headers=auth).status_code == 401
    assert client.post(prefix + "/login", json={"username": "admin", "password": "admin"}).status_code == 401
    assert (
        client.post(
            prefix + "/login", json={"username": "admin", "password": "upgraded-password-123"}
        ).status_code
        == 200
    )
