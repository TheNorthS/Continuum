import pytest
from sqlalchemy import select

from app.common.context import knowledge_scope
from app.infrastructure.container import Container
from app.models.entities import Chunk, Document


async def test_candidate_first_query_matches_original_acl_scope_status_and_revocation(settings, monkeypatch):
    services = Container(settings)
    try:
        await services.start()
        with services.db.session.begin() as session:
            for index, (base, status) in enumerate(
                (
                    ("a", "ready"),
                    ("a", "failed"),
                    ("b", "ready"),
                    ("b", "indexing"),
                    ("a", "deleting"),
                    ("hidden", "ready"),
                )
            ):
                session.add(
                    Document(
                        id=f"d{index}",
                        filename="test.txt",
                        digest=f"digest-{index}",
                        base_id=base,
                        status=status,
                    )
                )
            session.flush()
            for index in range(6):
                session.add(
                    Chunk(id=f"c{index}", document_id=f"d{index}", text="evidence", meta={}, embedding=[1.0])
                )
        ids = ["c0", "c1", "c2", "c3", "c4", "c5", "c0", "absent"]
        visible = ["a", "b"]
        monkeypatch.setattr(services.security, "visible_bases", lambda *args: visible.copy())
        for scope, expected in (
            (None, {"c0", "c2"}),
            (("a",), {"c0"}),
            (("b",), {"c2"}),
            (("hidden",), set()),
            ((), set()),
        ):
            token = knowledge_scope.set(scope)
            try:
                with services.db.session() as session:
                    original = set(
                        session.scalars(
                            select(Chunk.id)
                            .join(Document)
                            .where(
                                Document.status == "ready",
                                Chunk.id.in_(ids),
                                Document.id.in_(services.repository.allowed_documents(session)),
                            )
                        )
                    )
                assert services.repository.filter_ready(ids) == original == expected
            finally:
                knowledge_scope.reset(token)
        visible.clear()
        assert services.repository.filter_ready(ids) == set()
        assert services.repository.filter_ready([]) == set()
        monkeypatch.setattr(
            services.repository, "allowed_documents", lambda *_: (_ for _ in ()).throw(RuntimeError("no ACL"))
        )
        with pytest.raises(RuntimeError, match="no ACL"):
            services.repository.filter_ready(["c0"])
    finally:
        await services.close()
