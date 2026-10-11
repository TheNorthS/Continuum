import os

import pytest

from app.config import Settings
from app.main import create_app
from tests.signed_client import SignedClient as TestClient


@pytest.fixture(autouse=True)
def isolate_application_environment(monkeypatch):
    # Tests must not inherit developer credentials/configuration, including dotenv
    # variables loaded by an optional third-party dependency in an earlier test.
    # Explicit external-service test addresses remain available.
    for name in list(os.environ):
        if name.startswith("RAGE_") and not name.startswith("RAGE_TEST_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        encryption_key_file=tmp_path / "keyring.json",
        graph_enabled=False,
        langfuse_enabled=False,
        redis_url="",
        allow_registration=True,
        semantic_chunking=False,
        rate_limit_per_minute=1000,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def admin(client):
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "rag-e-demo-2026"})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


@pytest.fixture
def sample():
    return "# 芯片工艺\n\n## EUV 光刻\nEUV 光刻采用 13.5 纳米波长的极紫外光，用于制造先进芯片。\n\n## 封装\n芯片封装保护裸片，并提供电气连接和散热通道。".encode()
