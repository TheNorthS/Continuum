from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, event, select, text, update

from app.common.context import actor, knowledge_scope
from app.common.errors import AppError
from app.config import ROOT
from app.database.session import Database
from app.infrastructure.container import Container
from app.infrastructure.repositories import Repository
from app.knowledge.rag.local_store import LocalStore
from app.models.entities import Chunk, Document, Grant, KnowledgeBase, Role, SecurityState, User
from app.services.knowledge_service import KnowledgeService


def revision(repo, bases=None, user=None):
    principal = actor.set(user)
    scope = knowledge_scope.set(bases)
    try:
        return repo.revision()
    finally:
        knowledge_scope.reset(scope)
        actor.reset(principal)


def counter(db, base="legacy"):
    with db.session() as s:
        return s.scalar(select(KnowledgeBase.retrieval_revision).where(KnowledgeBase.id == base))


def add_base(db, name):
    with db.session.begin() as s:
        s.add(KnowledgeBase(id=name, name=name))


def commit(repo, key, base="legacy"):
    document, created = repo.reserve_document(key + ".txt", key, base)
    assert created
    cid = document["id"] + "_0"
    repo.commit_document(
        document["id"],
        [{"id": cid, "document_id": document["id"], "text": "old evidence", "meta": {}, "embedding": [1.0]}],
    )
    return document["id"], cid


@pytest.fixture
async def services(settings):
    value = Container(settings)
    await value.start()
    try:
        yield value
    finally:
        await value.close()


async def test_revision_tracks_ready_lifecycle_and_ignores_unpublished_work(services):
    repo, db = services.repository, services.db
    before, version = repo.revision(), counter(db)
    doc, _ = repo.reserve_document("pending.txt", "pending")
    assert repo.revision() == before
    with db.session.begin() as s:
        s.add(Chunk(id="pending-chunk", document_id=doc["id"], text="pending", meta={}, embedding=[1.0]))
    repo.fail_document(doc["id"])
    assert counter(db) == version
    assert repo.revision() == before
    repo.delete_document(doc["id"])
    assert repo.revision() == before
    ready, _ = commit(repo, "ready")
    assert counter(db) == version + 1  # Chunks are inserted before their document becomes ready.
    published = repo.revision()
    repo.mark_deleting(ready)
    assert counter(db) == version + 2 and repo.revision() != published
    deleting = repo.revision()
    repo.delete_document(ready)
    assert repo.revision() == deleting  # Already removed from the searchable view.
    failed, _ = commit(repo, "failure")
    published = repo.revision()
    repo.fail_document(failed)
    assert repo.revision() != published


async def test_in_place_updates_raw_sql_and_rollback_invalidate_transactionally(services):
    repo, db = services.repository, services.db
    did, cid = commit(repo, "in-place")
    before, version = repo.revision(), counter(db)
    with pytest.raises(RuntimeError, match="rollback"):
        with db.session.begin() as s:
            s.execute(update(Chunk).where(Chunk.id == cid).values(text="must roll back"))
            assert s.scalar(select(KnowledgeBase.retrieval_revision)) == version + 1
            raise RuntimeError("rollback")
    assert repo.revision() == before
    assert repo.ready_chunks()[0]["text"] == "old evidence"
    with db.session.begin() as s:
        s.execute(update(Chunk).where(Chunk.id == cid).values(text="new evidence"))
    assert counter(db) == version + 1 and repo.revision() != before
    # Direct SQL and a repeated no-op assignment also obey the same contract.
    before = repo.revision()
    with db.engine.begin() as c:
        c.execute(text("UPDATE documents SET filename=filename WHERE id=:id"), {"id": did})
    assert repo.revision() == before
    with db.engine.begin() as c:
        c.execute(text("UPDATE documents SET chunk_count=chunk_count+1 WHERE id=:id"), {"id": did})
    assert repo.revision() != before
    before = repo.revision()
    with db.session.begin() as s:
        s.execute(delete(Chunk).where(Chunk.id == cid))
    assert repo.revision() != before
    before = repo.revision()
    with db.session.begin() as s:
        s.add(Chunk(id=cid, document_id=did, text="restored", meta={}, embedding=[1.0]))
    assert repo.revision() != before
    before = repo.revision()
    with db.session.begin() as s:
        s.execute(delete(Document).where(Document.id == did))
    assert repo.revision() != before


async def test_content_invalidations_are_per_base_and_scope_order_is_canonical(services):
    repo, db = services.repository, services.db
    add_base(db, "a")
    add_base(db, "b")
    a_before = revision(repo, ("a",))
    b_before = revision(repo, ("b",))
    did, _ = commit(repo, "in-a", "a")
    assert revision(repo, ("a",)) != a_before
    assert revision(repo, ("b",)) == b_before
    assert revision(repo, ("a", "b")) == revision(repo, ("b", "a", "b"))
    before = {name: revision(repo, (name,)) for name in ("a", "b")}
    with db.session.begin() as s:
        s.execute(update(Document).where(Document.id == did).values(base_id="b"))
    assert all(revision(repo, (name,)) != before[name] for name in before)


async def test_acl_changes_and_regrant_never_reuse_an_old_generation(services):
    repo, db = services.repository, services.db
    add_base(db, "private")
    commit(repo, "private-doc", "private")
    with db.session.begin() as s:
        s.add(Role(id="reader", permissions=["knowledge.read"], builtin=False))
        s.add(User(id="reader", username="revisionreader", password_hash="test-hash", role="reader"))
    principal = {"id": "reader"}
    hidden = revision(repo, ("private",), principal)
    with db.session.begin() as s:
        s.add(Grant(base_id="private", subject_type="user", subject_id="reader", access="read"))
    granted = revision(repo, ("private",), principal)
    assert granted != hidden
    with db.session.begin() as s:
        s.execute(delete(Grant).where(Grant.base_id == "private"))
    revoked = revision(repo, ("private",), principal)
    with db.session.begin() as s:
        s.add(Grant(base_id="private", subject_type="user", subject_id="reader", access="read"))
    restored = revision(repo, ("private",), principal)
    assert len({hidden, granted, revoked, restored}) == 4
    for model, predicate, values in (
        (Role, Role.id == "reader", {"permissions": []}),
        (Role, Role.id == "reader", {"permissions": ["knowledge.read"]}),
        (User, User.id == "reader", {"active": False}),
        (User, User.id == "reader", {"active": True}),
    ):
        before = revision(repo, ("private",), principal)
        with db.session.begin() as s:
            s.execute(update(model).where(predicate).values(**values))
        assert revision(repo, ("private",), principal) != before
    before = repo.revision()
    with db.session.begin() as s:
        s.execute(
            update(SecurityState)
            .where(SecurityState.id == "policy")
            .values(version=SecurityState.version + 1)
        )
    assert repo.revision() == before  # Session/auth bookkeeping does not churn retrieval caches.


async def test_revision_is_bounded_and_does_not_read_document_or_chunk_tables(services):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(services.db.engine, "before_cursor_execute", record)
    try:
        value = services.repository.revision()
    finally:
        event.remove(services.db.engine, "before_cursor_execute", record)
    assert value.startswith("r3:") and len(value) == 67
    assert not any("documents" in statement or "chunks" in statement for statement in statements)


async def test_atomic_counter_increments_are_visible_to_other_repository_instances(services, settings):
    repo, db = services.repository, services.db
    did, _ = commit(repo, "concurrent")
    other_db = Database(settings.database_url, services.cipher)
    other = Repository(other_db)
    other.security = services.security
    before, version = other.revision(), counter(db)

    def change(_):
        with other_db.engine.begin() as c:
            c.execute(text("UPDATE documents SET chunk_count=chunk_count+1 WHERE id=:id"), {"id": did})

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(change, range(16)))
        assert counter(db) == version + 16
        assert other.revision() == repo.revision() != before
    finally:
        other_db.close()


def test_migration_preserves_existing_records_and_namespaces_databases(tmp_path):
    fingerprints = []
    for index in range(2):
        db = Database(f"sqlite:///{tmp_path / f'old-{index}.db'}")
        cfg = Config(str(ROOT / "backend/alembic.ini"))
        try:
            with db.engine.begin() as c:
                cfg.attributes["connection"] = c
                command.upgrade(cfg, "0005")
                c.execute(
                    text(
                        "INSERT INTO documents (id,filename,digest,status,chunk_count,created_at,graph_status,base_id) VALUES ('d','file','digest','ready',1,0,'disabled','legacy')"
                    )
                )
                c.execute(
                    text(
                        "INSERT INTO chunks (id,document_id,text,meta,embedding) VALUES ('c','d','text','{}','[1]')"
                    )
                )
                original = c.execute(text("SELECT * FROM documents")).all()
                command.upgrade(cfg, "head")
                assert c.execute(text("SELECT * FROM documents")).all() == original
                assert c.execute(text("SELECT text FROM chunks")).scalar() == "text"
                assert c.execute(text("SELECT retrieval_revision FROM knowledge_bases")).scalar() == 0
                command.check(cfg)
            repo = Repository(db)
            repo.security = Mock(visible_bases=Mock(return_value=["legacy"]))
            fingerprint = repo.revision()
            fingerprints.append(fingerprint)
            with db.engine.begin() as c:
                cfg.attributes["connection"] = c
                command.downgrade(cfg, "0005")
                command.upgrade(cfg, "head")
            assert repo.revision() != fingerprint  # Re-upgrade gets a fresh cache namespace.
        finally:
            db.close()
    assert fingerprints[0] != fingerprints[1]


def test_local_snapshot_retries_content_changes_and_does_not_publish_mixed_versions():
    repo = Mock()
    repo.revision.side_effect = ["a", "b", "b"]
    chunk = {"id": "new", "text": "evidence", "meta": {}, "embedding": [1.0]}
    repo.ready_chunks.side_effect = [[], [chunk]]
    store = LocalStore(repo)
    assert store.search("evidence", [1.0], 1)[0]["id"] == "new"
    assert store._revision == "b"
    repo.revision.side_effect = ["c", "d", "e", "f"]
    repo.ready_chunks.side_effect = [[], [], []]
    with pytest.raises(AppError) as exc:
        store.search("evidence", [1.0], 1)
    assert exc.value.code == "retrieval_changed"
    assert store._revision == "b"


async def test_candidate_cache_retries_generation_changes_and_preserves_final_acl(settings):
    repo = Mock()
    repo.revision.side_effect = ["a", "b", "b", "b"]
    repo.filter_ready.return_value = {"current"}
    cache = Mock(
        get_or_load=AsyncMock(
            side_effect=[[], [{"id": "current", "text": "text", "score": 1, "metadata": {}}]]
        )
    )
    service = KnowledgeService(settings, repo, None, None, None, cache)
    assert (await service.candidates("query", 1))[0]["id"] == "current"
    keys = [call.args[0] for call in cache.get_or_load.call_args_list]
    assert len(set(keys)) == 2
    repo.revision.side_effect = [str(i) for i in range(6)]
    cache.get_or_load.side_effect = [[], [], []]
    with pytest.raises(AppError) as exc:
        await service.candidates("query", 1)
    assert exc.value.code == "retrieval_changed"


async def test_in_place_chunk_change_rebuilds_local_snapshot_and_cache(services):
    repo = services.repository
    _, cid = commit(repo, "cached")
    services.embedder.embed = AsyncMock(return_value=[[1.0]])
    first = await services.knowledge.search("evidence", 1)
    assert first[0]["text"] == "old evidence"
    before = repo.revision()
    with services.db.session.begin() as s:
        s.execute(update(Chunk).where(Chunk.id == cid).values(text="updated evidence"))
    assert repo.revision() != before
    second = await services.knowledge.search("evidence", 1)
    assert second[0]["text"] == "updated evidence"
