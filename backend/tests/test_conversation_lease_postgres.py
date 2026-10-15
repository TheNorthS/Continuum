"""Run the same ownership contracts on disposable PostgreSQL databases in CI."""

import asyncio
import os

import pytest

from app.infrastructure.container import Container
from tests.test_conversation_lease import concurrent_commit_contract, expiry_and_takeover_contract
from tests.test_retrieval_revision_postgres import postgres_settings  # noqa: F401

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(not os.getenv("RAGE_TEST_POSTGRES_URL"), reason="test PostgreSQL not configured"),
]


@pytest.mark.parametrize("contract", [expiry_and_takeover_contract, concurrent_commit_contract])
async def test_postgres_conversation_lease(postgres_settings, contract):  # noqa: F811
    services = Container(postgres_settings)
    try:
        await services.start()
        await asyncio.to_thread(contract, services)
    finally:
        await services.close()
