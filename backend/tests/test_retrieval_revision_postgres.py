"""The same revision contract on the production SQL dialect, using disposable databases."""

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import ROOT
from app.database.session import Database
from app.infrastructure.container import Container
from tests import test_retrieval_revision as contract

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(not os.getenv("RAGE_TEST_POSTGRES_URL"), reason="test PostgreSQL not configured"),
]


@pytest.fixture
def postgres_settings(settings):
    import psycopg
    from psycopg import sql

    url = os.environ["RAGE_TEST_POSTGRES_URL"]
    database = "rage_revision_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    parsed = urlsplit(url)
    settings.database_url = urlunsplit(
        ("postgresql+psycopg", parsed.netloc, "/" + database, parsed.query, parsed.fragment)
    )
    try:
        yield settings
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


@pytest.mark.parametrize(
    "scenario",
    [
        contract.test_revision_tracks_ready_lifecycle_and_ignores_unpublished_work,
        contract.test_in_place_updates_raw_sql_and_rollback_invalidate_transactionally,
        contract.test_content_invalidations_are_per_base_and_scope_order_is_canonical,
        contract.test_acl_changes_and_regrant_never_reuse_an_old_generation,
        contract.test_revision_is_bounded_and_does_not_read_document_or_chunk_tables,
        contract.test_in_place_chunk_change_rebuilds_local_snapshot_and_cache,
    ],
    ids=lambda scenario: scenario.__name__.removeprefix("test_"),
)
async def test_postgres_revision_contract(postgres_settings, scenario):
    services = Container(postgres_settings)
    try:
        await services.start()
        await scenario(services)
    finally:
        await services.close()


async def test_postgres_concurrent_revision_updates(postgres_settings):
    services = Container(postgres_settings)
    try:
        await services.start()
        await contract.test_atomic_counter_increments_are_visible_to_other_repository_instances(
            services, postgres_settings
        )
    finally:
        await services.close()


def test_postgres_upgrade_from_existing_schema_and_downgrade(postgres_settings):
    db = Database(postgres_settings.database_url)
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
            before = c.execute(text("SELECT * FROM documents")).all()
            command.upgrade(cfg, "head")
            assert c.execute(text("SELECT * FROM documents")).all() == before
            assert c.execute(text("SELECT retrieval_revision FROM knowledge_bases")).scalar() == 0
            command.check(cfg)
            c.execute(text("UPDATE chunks SET text='new' WHERE id='c'"))
            assert c.execute(text("SELECT retrieval_revision FROM knowledge_bases")).scalar() == 1
            namespace = c.execute(
                text("SELECT signature FROM index_profiles WHERE key='retrieval_cache'")
            ).scalar()
            command.downgrade(cfg, "0005")
            command.upgrade(cfg, "head")
            assert (
                c.execute(text("SELECT signature FROM index_profiles WHERE key='retrieval_cache'")).scalar()
                != namespace
            )
            assert c.execute(text("SELECT text FROM chunks WHERE id='c'")).scalar() == "new"
    finally:
        db.close()
