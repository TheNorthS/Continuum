"""Lease expiry, renewal, fencing and cancellation against isolated SQL storage."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.common.errors import AppError
from app.config import Settings
from app.infrastructure.container import Container
from app.infrastructure.conversation_lease import ConversationLease
from app.models.entities import Conversation, MemoryEvent, MemoryJob, Message


@pytest.fixture
async def lease_services(settings):
    services = Container(settings)
    try:
        await services.start()
        yield services
    finally:
        await services.close()


def new_conversation(services):
    uid = services.repository.user(username="admin")["id"]
    return uid, services.repository.create_conversation(uid, "新对话", use_knowledge=False)["id"]


def expire(services, cid):
    with services.db.session.begin() as session:
        session.execute(
            update(Conversation)
            .where(Conversation.id == cid)
            .values(lock_until=services.repository.lease_now() - 1)
        )


def answer_result():
    return {"answer": "safe reply", "sources": [], "rewrites": 0, "generations": 1, "status": "answered"}


def assert_expired(call):
    with pytest.raises(AppError) as failure:
        call()
    assert failure.value.code == "conversation_expired"


def expiry_and_takeover_contract(services):
    repo = services.repository
    uid, cid = new_conversation(services)
    old = repo.claim(cid, uid)
    expire(services, cid)
    # Even without a successor, expiry alone removes the old holder's rights.
    assert_expired(lambda: repo.renew(cid, old))
    assert_expired(lambda: repo.finish(cid, old, "stale", answer_result()))
    assert_expired(lambda: repo.configure_scope(cid, uid, old, use_knowledge=True))
    assert_expired(lambda: repo.configure_attachments(cid, uid, old, []))
    fresh = repo.claim(cid, uid)
    assert fresh != old
    repo.release(cid, old)
    assert_expired(lambda: repo.renew(cid, old))
    assert_expired(lambda: repo.finish(cid, old, "stale", answer_result()))
    repo.renew(cid, fresh)
    repo.finish(cid, fresh, "new owner", answer_result())
    # A successful commit consumes the capability, including repeated calls from the same holder.
    assert_expired(lambda: repo.finish(cid, fresh, "duplicate", answer_result()))
    with services.db.session() as session:
        row = session.get(Conversation, cid)
        assert row.lock_token is None and row.lock_until == 0
        assert row.title == "new owner"
        assert (
            session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == cid))
            == 2
        )
        assert (
            session.scalar(
                select(func.count()).select_from(MemoryEvent).where(MemoryEvent.conversation_id == cid)
            )
            == 2
        )
    next_token = repo.claim(cid, uid)
    repo.release(cid, fresh)
    repo.renew(cid, next_token)
    repo.release(cid, next_token)


def concurrent_commit_contract(services):
    repo = services.repository
    uid, cid = new_conversation(services)
    barrier = threading.Barrier(2)

    def claim(_):
        barrier.wait(timeout=5)
        try:
            return repo.claim(cid, uid)
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert claims.count("conversation_busy") == 1
    token = next(c for c in claims if c != "conversation_busy")
    barrier = threading.Barrier(2)

    def finish(index):
        barrier.wait(timeout=5)
        try:
            repo.finish(cid, token, f"winner {index}", answer_result())
            return "committed"
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    assert sorted(results) == ["committed", "conversation_expired"]
    with services.db.session() as session:
        assert (
            session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == cid))
            == 2
        )
        assert session.get(Conversation, cid).context_revision == 1


async def test_expiry_takeover_and_consumed_token(lease_services):
    await asyncio.to_thread(expiry_and_takeover_contract, lease_services)


async def test_concurrent_claim_and_duplicate_commit(lease_services):
    await asyncio.to_thread(concurrent_commit_contract, lease_services)


async def test_expiry_during_finish_rolls_back_messages_events_jobs_and_title(lease_services, monkeypatch):
    services = lease_services
    services.settings.memory_min_turns = 1
    uid, cid = new_conversation(services)
    token = services.repository.claim(cid, uid)
    original = services.memory_store.on_finish

    def expire_in_transaction(session, row):
        original(session, row)
        row.lock_until = 0

    monkeypatch.setattr(services.memory_store, "on_finish", expire_in_transaction)
    assert_expired(lambda: services.repository.finish(cid, token, "not committed", answer_result()))
    with services.db.session() as session:
        row = session.get(Conversation, cid)
        assert row.title == "新对话" and row.context_revision == 0
        for model in (Message, MemoryEvent, MemoryJob):
            assert (
                session.scalar(select(func.count()).select_from(model).where(model.conversation_id == cid))
                == 0
            )
    services.repository.release(cid, token)


async def test_lease_clock_is_database_not_worker_clock(lease_services, monkeypatch):
    from app.infrastructure import repositories

    def forbidden_clock():
        raise AssertionError("worker clock used for lease")

    monkeypatch.setattr(repositories, "time", SimpleNamespace(time=forbidden_clock))
    await asyncio.to_thread(expiry_and_takeover_contract, lease_services)


async def test_long_generation_renews_and_keeps_competitors_out(lease_services, monkeypatch):
    services = lease_services
    # Runtime overrides keep this test short; production settings enforce a safe renewal ratio.
    services.repository.lease_seconds = 1
    services.settings.conversation_lease_renew_interval = 0.05
    uid, cid = new_conversation(services)
    renewals = []
    renew = services.repository.renew

    def counted(cid, token):
        renew(cid, token)
        renewals.append(token)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1.1)
        with pytest.raises(AppError) as failure:
            await asyncio.to_thread(services.repository.claim, cid, uid)
        assert failure.value.code == "conversation_busy"
        return answer_result()

    monkeypatch.setattr(services.repository, "renew", counted)
    monkeypatch.setattr(services.orchestrator, "run", slow)
    result = await services.chat.answer(cid, uid, "hello", "adaptive")
    assert result["answer"] == "safe reply" and len(renewals) >= 2
    assert len(services.repository.history(cid, uid)) == 2


@pytest.mark.parametrize("failure_code", ["conversation_expired", "database_unavailable"])
async def test_renew_failure_cancels_generation_without_commit(lease_services, monkeypatch, failure_code):
    services = lease_services
    services.settings.conversation_lease_renew_interval = 0.05
    uid, cid = new_conversation(services)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    renew = services.repository.renew

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def fail_after_entry(cid, token):
        if not entered.is_set():
            return renew(cid, token)
        if failure_code == "conversation_expired":
            raise AppError(failure_code, "lost", 409)
        raise OSError("private database detail")

    monkeypatch.setattr(services.orchestrator, "run", blocked)
    monkeypatch.setattr(services.repository, "renew", fail_after_entry)
    with pytest.raises(AppError) as failure:
        await asyncio.wait_for(services.chat.answer(cid, uid, "hello", "adaptive"), timeout=5)
    assert failure.value.code == (
        "conversation_expired" if failure_code == "conversation_expired" else "conversation_lease_unavailable"
    )
    assert cancelled.is_set() and services.repository.history(cid, uid) == []
    token = services.repository.claim(cid, uid)
    services.repository.release(cid, token)


@pytest.mark.parametrize("wait_for_heartbeat", [True, False])
async def test_successor_survives_old_request_cleanup(lease_services, monkeypatch, wait_for_heartbeat):
    services = lease_services
    services.settings.conversation_lease_renew_interval = 0.05
    uid, cid = new_conversation(services)
    successor = []

    async def takeover(*args, **kwargs):
        await asyncio.to_thread(expire, services, cid)
        successor.append(await asyncio.to_thread(services.repository.claim, cid, uid))
        if wait_for_heartbeat:
            await asyncio.Event().wait()
        return answer_result()

    monkeypatch.setattr(services.orchestrator, "run", takeover)
    with pytest.raises(AppError) as failure:
        await asyncio.wait_for(services.chat.answer(cid, uid, "hello", "adaptive"), timeout=5)
    assert failure.value.code == "conversation_expired"
    services.repository.renew(cid, successor[0])
    services.repository.finish(cid, successor[0], "successor", answer_result())
    assert len(services.repository.history(cid, uid)) == 2


async def test_cancelled_generation_releases_lease_and_stops_renewing(lease_services, monkeypatch):
    services = lease_services
    services.settings.conversation_lease_renew_interval = 0.05
    uid, cid = new_conversation(services)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(services.orchestrator, "run", blocked)
    task = asyncio.create_task(services.chat.answer(cid, uid, "hello", "adaptive"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and services.repository.history(cid, uid) == []
    token = services.repository.claim(cid, uid)
    # No old heartbeat may release or overwrite the new holder after cleanup.
    await asyncio.sleep(0.12)
    services.repository.renew(cid, token)
    services.repository.release(cid, token)


async def test_timed_out_renewal_cannot_revive_released_or_successor_lease(lease_services, monkeypatch):
    services = lease_services
    services.settings.conversation_lease_renew_interval = 0.05
    uid, cid = new_conversation(services)
    entered, resume, ended = threading.Event(), threading.Event(), threading.Event()
    original = services.repository.renew
    late_results = []

    def slow_renew(cid, token):
        entered.set()
        try:
            assert resume.wait(5)
            original(cid, token)
            late_results.append("renewed")
        except AppError as exc:
            late_results.append(exc.code)
            raise
        finally:
            ended.set()

    async def operation(lease):
        await asyncio.Event().wait()

    monkeypatch.setattr(services.repository, "renew", slow_renew)
    lease = ConversationLease(services.repository, services.settings, cid, uid)
    try:
        with pytest.raises(AppError) as failure:
            await asyncio.wait_for(lease.run(operation), timeout=3)
        assert entered.is_set() and failure.value.code == "conversation_lease_unavailable"
        successor = services.repository.claim(cid, uid)
    finally:
        resume.set()
    assert await asyncio.to_thread(ended.wait, 5)
    assert late_results == ["conversation_expired"]
    original(cid, successor)
    services.repository.release(cid, successor)


async def test_cancellation_during_claim_drains_and_releases_late_claim(lease_services, monkeypatch):
    services = lease_services
    uid, cid = new_conversation(services)
    claimed, resume = threading.Event(), threading.Event()
    original = services.repository.claim
    ran = False

    def delayed_claim(cid, uid):
        token = original(cid, uid)
        claimed.set()
        assert resume.wait(5)
        return token

    async def operation(lease):
        nonlocal ran
        ran = True

    monkeypatch.setattr(services.repository, "claim", delayed_claim)
    task = asyncio.create_task(
        ConversationLease(services.repository, services.settings, cid, uid).run(operation)
    )
    try:
        assert await asyncio.to_thread(claimed.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ran
    token = original(cid, uid)
    services.repository.release(cid, token)


def test_renew_interval_requires_margin():
    with pytest.raises(ValidationError, match="三分之一"):
        Settings(_env_file=None, conversation_lease_seconds=30, conversation_lease_renew_interval=20)


def test_manual_compaction_rejects_expired_lease_without_installing_checkpoint(client, admin, monkeypatch):
    from tests.test_memory_context import P, create, seed

    services = client.app.state.container
    cid = create(client, admin)
    seed(services, cid)
    before = client.get(P + f"/conversations/{cid}/messages", headers=admin).json()
    original = services.memory_store.commit_checkpoint

    def expire_before_commit(snapshot, *args):
        expire(services, snapshot["conversation_id"])
        return original(snapshot, *args)

    monkeypatch.setattr(services.memory_store, "commit_checkpoint", expire_before_commit)
    response = client.post(P + f"/conversations/{cid}/context/compact", headers=admin)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conversation_expired"
    assert client.get(P + f"/conversations/{cid}/context", headers=admin).json()["checkpoint"] is None
    assert client.get(P + f"/conversations/{cid}/messages", headers=admin).json() == before
