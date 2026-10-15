"""Exercise memory SQL fencing on production PostgreSQL when explicitly provisioned."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.common.errors import AppError
from app.infrastructure.container import Container
from app.models.entities import MemoryRecord
from tests.test_retrieval_revision_postgres import postgres_settings  # noqa: F401

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(not os.getenv("RAGE_TEST_POSTGRES_URL"), reason="test PostgreSQL not configured"),
]


async def test_postgres_memory_versions_job_claim_and_context_cas(postgres_settings):  # noqa: F811
    services = Container(postgres_settings)
    try:
        await services.start()
        uid = services.repository.user(username="admin")["id"]
        payload = {
            "subject": "Project",
            "predicate": "database",
            "value": "PostgreSQL",
            "category": "fact",
            "base_id": None,
        }
        first = await services.memory.manual(uid, payload)
        second = await services.memory.manual(
            uid, {**payload, "value": "SQLite"}, memory_id=first["id"], expected_version=1
        )
        assert second["version"] == 2
        with services.db.session() as session:
            rows = session.scalars(select(MemoryRecord).where(MemoryRecord.owner_id == uid)).all()
            assert {r.status for r in rows} == {"active", "superseded"}
        conversation = services.repository.create_conversation(uid, "task", use_knowledge=False)
        cid = conversation["id"]
        lease = services.repository.claim(cid, uid)
        try:
            services.repository.finish(
                cid,
                lease,
                "Project uses SQLite",
                {"answer": "acknowledged", "sources": [], "generations": 1, "rewrites": 0},
            )
        finally:
            services.repository.release(cid, lease)
        services.memory.enqueue(uid, cid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: services.memory.claim(), range(2)))
        assert sum(c is not None for c in claims) == 1
        job = next(c for c in claims if c)
        services.memory.stage_candidates(job, [])
        services.memory.consolidate(job, [])
        services.memory_store.forget(uid, second["id"])
        assert await asyncio.to_thread(services.memory_store.list_records, uid) == []
        with pytest.raises(AppError):
            services.memory.stage_candidates(job, [])
    finally:
        await services.close()
