import json
from unittest.mock import AsyncMock

import pytest

from app.evaluation.datasets import Dataset
from app.evaluation.vector_reuse import import_seed, load_seed
from app.infrastructure.container import Container


async def test_vector_reuse_preserves_exact_values_and_rejects_changed_embedding(
    settings, tmp_path, monkeypatch
):
    source = tmp_path / "old-run"
    source.mkdir()
    settings.database_url = "sqlite:///" + (source / ".runtime/evaluation.db").as_posix()
    settings.encryption_key_file = source / ".runtime/keyring.json"
    services = Container(settings)
    text = "# EUV\nEUV 波长是13.5纳米。"
    dataset = Dataset("test", {"one": text}, [{"id": "q"}], {})
    try:
        await services.start()
        document = await services.knowledge.ingest("source.txt", text.encode())
        original = services.repository.ready_chunks()
        (source / "dataset.json").write_text(
            json.dumps({"corpus": dataset.corpus, "queries": dataset.queries}), encoding="utf-8"
        )
        (source / "document-map.json").write_text(json.dumps({"one": document["id"]}), encoding="utf-8")
        (source / "manifest.json").write_text(
            json.dumps(
                {
                    "contract": {
                        "configuration": {
                            key: getattr(settings, key)
                            for key in (
                                "chunk_size",
                                "chunk_overlap",
                                "semantic_chunking",
                                "embedding_provider",
                                "embedding_model",
                                "embedding_dimension",
                            )
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    finally:
        await services.close()
    seeded, evidence = load_seed(source, dataset, settings)
    assert evidence["document_embedding_api_calls"] == 0
    assert seeded["one"]["chunks"][0]["embedding"] == original[0]["embedding"]
    settings.database_url = "sqlite:///" + (tmp_path / "new.db").as_posix()
    settings.encryption_key_file = tmp_path / "new-key.json"
    target = Container(settings)
    try:
        await target.start()
        monkeypatch.setattr(
            target.embedder, "embed", AsyncMock(side_effect=AssertionError("Should reuse vectors"))
        )
        row = await import_seed(target, seeded["one"])
        cloned = target.repository.ready_chunks()
        assert row["id"] != document["id"]
        assert cloned[0]["embedding"] == original[0]["embedding"]
        assert cloned[0]["meta"]["document_id"] == row["id"]
        target.embedder.embed.assert_not_awaited()
    finally:
        await target.close()
    # Changing the evaluation question/corpus selection may reuse only the
    # intersection of already committed documents, with identical implementation.
    from app.evaluation.checkpoint import Checkpoint
    from app.evaluation.implementation import pin_implementation

    pin_implementation(source)
    checkpoint = Checkpoint(source)
    checkpoint.close()
    subset = Dataset("changed-scope", {"one": text, "new": "new content"}, [{"id": "different"}], {})
    from pathlib import Path

    original_read = Path.read_text

    def reject_full_source_read(path, *args, **kwargs):
        if path == source / "dataset.json":
            raise AssertionError("Partial reuse must not load the entire source corpus")
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_text", reject_full_source_read)
        partial, evidence = load_seed(source, subset, settings, matching_only=True)
    assert set(partial) == {"one"}
    assert partial["one"]["chunks"][0]["embedding"] == original[0]["embedding"]
    assert evidence["mode"] == "matching_completed_documents"
    subset.corpus["one"] = "changed source text"
    with pytest.raises(ValueError, match="content mismatch"):
        load_seed(source, subset, settings, matching_only=True)
    settings.embedding_dimension = 256
    with pytest.raises(ValueError, match="configuration mismatch"):
        load_seed(source, dataset, settings)


def test_milvus_verification_waits_for_finished_indexes(settings, monkeypatch):
    from unittest.mock import Mock

    from app.evaluation.vector_reuse import verify_milvus

    services = Mock(settings=settings)
    services.repository.db.engine.dialect.name = "postgresql"
    services.repository.ready_chunk_ids.return_value = ["chunk"]
    services.store.client.query.side_effect = [[{"count(*)": 1}], [{"id": "chunk"}]]
    services.store.client.list_indexes.return_value = ["dense"]
    services.store.client.describe_index.side_effect = [
        {"state": "InProgress", "pending_index_rows": 1},
        {"state": "Finished", "pending_index_rows": 0, "indexed_rows": 1},
    ]
    monkeypatch.setattr("app.evaluation.vector_reuse.time.sleep", Mock())
    evidence = verify_milvus(services)
    assert evidence["index_detail"]["dense"]["state"] == "Finished"
    assert services.store.client.describe_index.call_count == 2
