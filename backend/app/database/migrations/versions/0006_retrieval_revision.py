"""Transactional per-knowledge-base invalidation, including direct SQL chunk updates."""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = depends_on = None

DOCUMENT_FIELDS = ("id", "base_id", "status", "filename", "digest", "chunk_count")
CHUNK_FIELDS = ("id", "document_id", "text", "meta", "embedding")
ACL_FIELDS = {
    "resource_grants": ("base_id", "subject_type", "subject_id", "access"),
    "roles": ("id", "permissions"),
    "users": ("id", "role", "active"),
    "knowledge_bases": ("id", "owner_id"),
}


def sqlite_triggers():
    for operation, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
        op.execute(f"""CREATE TRIGGER rage_documents_revision_{operation.lower()}
            AFTER {operation} ON documents WHEN {row}.status = 'ready'
            BEGIN
                UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                WHERE id = {row}.base_id;
            END""")
        op.execute(f"""CREATE TRIGGER rage_chunks_revision_{operation.lower()}
            AFTER {operation} ON chunks
            BEGIN
                UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                WHERE id IN (SELECT base_id FROM documents
                             WHERE id = {row}.document_id AND status = 'ready');
            END""")
    changed = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in DOCUMENT_FIELDS)
    op.execute(f"""CREATE TRIGGER rage_documents_revision_update
        AFTER UPDATE OF {",".join(DOCUMENT_FIELDS)} ON documents WHEN {changed}
        BEGIN
            UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
            WHERE (id = OLD.base_id AND OLD.status = 'ready')
               OR (id = NEW.base_id AND NEW.status = 'ready');
        END""")
    changed = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in CHUNK_FIELDS)
    op.execute(f"""CREATE TRIGGER rage_chunks_revision_update
        AFTER UPDATE OF {",".join(CHUNK_FIELDS)} ON chunks WHEN {changed}
        BEGIN
            UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
            WHERE id IN (SELECT base_id FROM documents
                         WHERE id IN (OLD.document_id, NEW.document_id) AND status = 'ready');
        END""")


def postgres_triggers():
    old_document = ",".join("OLD." + field for field in DOCUMENT_FIELDS)
    new_document = ",".join("NEW." + field for field in DOCUMENT_FIELDS)
    op.execute(f"""CREATE FUNCTION rage_documents_revision() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status = 'ready' THEN
                    UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                    WHERE id = NEW.base_id;
                END IF;
            ELSIF TG_OP = 'DELETE' THEN
                IF OLD.status = 'ready' THEN
                    UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                    WHERE id = OLD.base_id;
                END IF;
            ELSIF ROW({old_document}) IS DISTINCT FROM ROW({new_document}) THEN
                UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                WHERE (id = OLD.base_id AND OLD.status = 'ready')
                   OR (id = NEW.base_id AND NEW.status = 'ready');
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql""")
    old_chunk = ",".join("OLD." + field for field in CHUNK_FIELDS)
    new_chunk = ",".join("NEW." + field for field in CHUNK_FIELDS)
    op.execute(f"""CREATE FUNCTION rage_chunks_revision() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                WHERE id IN (SELECT base_id FROM documents
                             WHERE id = NEW.document_id AND status = 'ready');
            ELSIF TG_OP = 'DELETE' THEN
                UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                WHERE id IN (SELECT base_id FROM documents
                             WHERE id = OLD.document_id AND status = 'ready');
            ELSIF ROW({old_chunk}) IS DISTINCT FROM ROW({new_chunk}) THEN
                UPDATE knowledge_bases SET retrieval_revision = retrieval_revision + 1
                WHERE id IN (SELECT base_id FROM documents
                             WHERE id IN (OLD.document_id, NEW.document_id) AND status = 'ready');
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql""")
    for table in ("documents", "chunks"):
        op.execute(f"""CREATE TRIGGER rage_{table}_revision
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION rage_{table}_revision()""")


def acl_triggers(dialect):
    # Permission changes are much less frequent than document ingestion. A separate
    # global epoch fences revocation/regrant (ABA) and role changes during cache loads,
    # without invalidating retrieval for logins, nonces or unrelated audit events.
    for table, fields in ACL_FIELDS.items():
        if dialect == "sqlite":
            changed = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in fields)
            for operation in ("INSERT", "UPDATE", "DELETE"):
                clause = f"UPDATE OF {','.join(fields)}" if operation == "UPDATE" else operation
                condition = f"WHEN {changed}" if operation == "UPDATE" else ""
                op.execute(f"""CREATE TRIGGER rage_{table}_acl_{operation.lower()}
                    AFTER {clause} ON {table} {condition}
                    BEGIN
                        UPDATE security_state SET version = version + 1 WHERE id = 'retrieval_acl';
                    END""")
        else:
            # PostgreSQL JSON (role permissions) lacks equality; compare its text representation.
            previous = ",".join(f"OLD.{field}::text" for field in fields)
            current = ",".join(f"NEW.{field}::text" for field in fields)
            op.execute(f"""CREATE FUNCTION rage_{table}_acl() RETURNS trigger AS $$
                BEGIN
                    IF TG_OP = 'UPDATE' THEN
                        IF ROW({previous}) IS NOT DISTINCT FROM ROW({current}) THEN
                            RETURN NULL;
                        END IF;
                    END IF;
                    UPDATE security_state SET version = version + 1 WHERE id = 'retrieval_acl';
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql""")
            op.execute(f"""CREATE TRIGGER rage_{table}_acl
                AFTER INSERT OR UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION rage_{table}_acl()""")


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError("Retrieval revision triggers support SQLite and PostgreSQL only")
    op.add_column(
        "knowledge_bases",
        sa.Column("retrieval_revision", sa.BigInteger, nullable=False, server_default="0"),
    )
    # One persisted namespace per database prevents counters from colliding across deployments.
    profiles = sa.table("index_profiles", sa.column("key", sa.String), sa.column("signature", sa.JSON))
    op.bulk_insert(profiles, [{"key": "retrieval_cache", "signature": {"namespace": uuid.uuid4().hex}}])
    op.execute("INSERT INTO security_state (id,version,value) VALUES ('retrieval_acl',0,'')")
    if dialect == "sqlite":
        sqlite_triggers()
    else:
        postgres_triggers()
    acl_triggers(dialect)


def downgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in ("documents", "chunks"):
            for operation in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER rage_{table}_revision_{operation}")
    else:
        for table in ("documents", "chunks"):
            op.execute(f"DROP TRIGGER rage_{table}_revision ON {table}")
            op.execute(f"DROP FUNCTION rage_{table}_revision()")
    for table in ACL_FIELDS:
        if dialect == "sqlite":
            for operation in ("insert", "update", "delete"):
                op.execute(f"DROP TRIGGER rage_{table}_acl_{operation}")
        else:
            op.execute(f"DROP TRIGGER rage_{table}_acl ON {table}")
            op.execute(f"DROP FUNCTION rage_{table}_acl()")
    op.execute("DELETE FROM index_profiles WHERE key = 'retrieval_cache'")
    op.execute("DELETE FROM security_state WHERE id = 'retrieval_acl'")
    op.drop_column("knowledge_bases", "retrieval_revision")
