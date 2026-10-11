import json

import pytest

from app.common.context import knowledge_scope
from app.evaluation.checkpoint import Checkpoint, RunLock, check_capacity, safe_error
from app.evaluation.datasets import Dataset
from app.evaluation.metrics import latency, retrieval_metrics
from app.evaluation.run import make_report
from app.infrastructure.container import Container


def test_full_crud_uses_every_partition_and_question_without_injected_answers(tmp_path):
    from app.evaluation.full_data import full_crud

    folder = tmp_path / "crud/full-corpus"
    folder.mkdir(parents=True)
    for part in range(1, 16):
        for section in range(1, 4):
            (folder / f"documents_dup_part_{part}_part_{section}").write_text(
                "official news\n", encoding="utf-8"
            )
    data = {
        task: [
            {
                "ID": "1",
                "questions": "Question",
                "answers": "DO NOT INDEX ANSWER",
                "news1": "DO NOT INJECT SOURCE",
            }
        ]
        for task in ("questanswer_1doc", "questanswer_2docs", "questanswer_3docs")
    }
    (tmp_path / "crud/split_merged.json").write_text(json.dumps(data), encoding="utf-8")
    dataset = full_crud(tmp_path)
    assert len(dataset.queries) == 3
    assert list(dataset.corpus.values()) == ["official news"]
    assert dataset.metadata["physical_news_records"] == 45
    assert dataset.metadata["exact_duplicate_records"] == 44
    assert all(not q["relevant"] for q in dataset.queries)
    assert len({q["group"] for q in dataset.queries}) == 1  # Keep correlated task variants in full mode.


def test_publisher_refuses_partial_or_mismatched_runs_before_touching_readme(tmp_path):
    from app.evaluation.publish import EXPECTED, RUNS, publish, validated_results

    (tmp_path / "README.md").write_text("user content", encoding="utf-8")
    for kind, name in RUNS.items():
        directory = tmp_path / "test-results/evaluation" / name
        directory.mkdir(parents=True)
        questions, documents = EXPECTED[kind]
        files = {
            "metrics.json": {
                "status": "complete",
                "full_dataset": True,
                "completed_queries": questions,
                "planned_queries": questions,
                "corpus_size": documents,
                "vector_backend": "milvus",
                "pipeline_fingerprint": kind,
            },
            "manifest.json": {"contract": {"dataset": {"full": True}}},
            "index.json": {"documents": documents, "chunks": documents},
            "milvus-index.json": {"chunks_verified": documents},
            "implementation.json": {"fingerprint": "same-source"},
        }
        for name, value in files.items():
            (directory / name).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="different pipeline"):
        validated_results(tmp_path)
    path = tmp_path / "test-results/evaluation" / RUNS["crud"] / "metrics.json"
    value = json.loads(path.read_text())
    value.update(completed_queries=1, pipeline_fingerprint="t2")
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="incomplete"):
        publish(tmp_path)
    assert (tmp_path / "README.md").read_text() == "user content"
    assert not (tmp_path / "docs/assets").exists()

    # A completed subset must be accepted only when the explicit plan calls for it,
    # never silently presented as the full corpus.
    plan = {
        "runs": {
            kind: {
                "run_name": name,
                "questions": EXPECTED[kind][0],
                "documents": EXPECTED[kind][1],
                "full": True,
            }
            for kind, name in RUNS.items()
        }
    }
    value.update(completed_queries=EXPECTED["crud"][0])
    path.write_text(json.dumps(value))
    directory = tmp_path / "test-results/evaluation" / RUNS["t2"]
    metrics = json.loads((directory / "metrics.json").read_text())
    metrics.update(full_dataset=False, completed_queries=1000, planned_queries=1000, corpus_size=10000)
    (directory / "metrics.json").write_text(json.dumps(metrics))
    (directory / "manifest.json").write_text(json.dumps({"contract": {"dataset": {"full": False}}}))
    (directory / "index.json").write_text(json.dumps({"documents": 10000, "chunks": 10000}))
    (directory / "milvus-index.json").write_text(json.dumps({"chunks_verified": 10000}))
    with pytest.raises(ValueError, match="incomplete"):
        validated_results(tmp_path)
    plan["runs"]["t2"].update(full=False, questions=1000, documents=10000)
    assert validated_results(tmp_path, plan)["t2"]["corpus_size"] == 10000


def test_explicit_evaluation_plan_pins_new_scope_and_rejects_paths(tmp_path):
    from app.evaluation.plan import load_plan

    value = load_plan()
    assert value["runs"]["t2"]["documents"] == 10000
    assert value["runs"]["t2"]["questions"] == 1000
    assert not value["runs"]["t2"]["full"]
    assert value["runs"]["crud"]["full"]
    value["runs"]["t2"]["run_name"] = "../outside"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="scope"):
        load_plan(path)


def test_publisher_keeps_public_links_and_detailed_charts_out_of_readme(tmp_path, monkeypatch):
    from app.config import ROOT
    from app.evaluation import publish as publisher

    results = json.loads((ROOT / "docs/assets/evaluation/metrics.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(publisher, "validated_results", lambda *args: results)
    monkeypatch.setattr(publisher, "charts", lambda results, directory: directory.mkdir(parents=True))
    (tmp_path / "docs").mkdir()
    original = f"intro\n{publisher.START}\nold\n{publisher.END}\nlimitations remain"
    for name in ("README.md", "docs/FULL_EVALUATION.md"):
        (tmp_path / name).write_text(original, encoding="utf-8")
    publisher.publish(tmp_path)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    report = (tmp_path / "docs/FULL_EVALUATION.md").read_text(encoding="utf-8")
    assert readme.startswith("intro") and readme.endswith("limitations remain")
    assert report.endswith("limitations remain")
    assert "latency.png" not in readme and "rerank-delta.png" not in readme
    assert "(assets/evaluation/latency.png)" in report
    assert "45/48" in report
    assert "CRUD" not in readme and "answers.png" not in readme
    assert "other-machine" not in readme and "测试成绩.md" not in readme
    assert "docs/assets/evaluation/retrieval.png" in readme
    assert "0.6958" in readme and "5.08" in readme
    assert "test-results/" not in readme + report


def test_checkpoint_resumes_committed_documents_and_answers(tmp_path):
    (tmp_path / "document-map.json").write_text(json.dumps({"a": "old"}))
    journal = Checkpoint(tmp_path)
    journal.put("documents", "b", "new")
    journal.put("results", "q", {"status": "error"})
    journal.close()
    resumed = Checkpoint(tmp_path)
    try:
        assert resumed.read("documents") == {"a": "old", "b": "new"}
        assert resumed.read("results") == {"q": {"status": "error"}}
    finally:
        resumed.close()


def test_run_lock_prevents_duplicate_writers_and_releases(tmp_path):
    with RunLock(tmp_path):
        with pytest.raises(RuntimeError, match="already_running"), RunLock(tmp_path):
            pass
    with RunLock(tmp_path):
        pass


def test_full_crud_missing_qrels_are_unmeasured_not_zero(tmp_path):
    assert retrieval_metrics({}, ["doc"]) == {}
    query = {"id": "q", "relevant": {}}
    result = {"q": {v: {"retrieval_metrics": {}, "ranking": ["doc"]} for v in ("rrf", "rerank")}}
    dataset = Dataset("CRUD-full", {"doc": "text"}, [query], {"full": True})
    report = make_report(tmp_path, dataset, result, "complete")
    assert not report["retrieval_judgments_available"]
    assert report["variants"]["rerank"]["retrieval"] == {}
    assert report["paired_retrieval"]["recall@4"] == {}
    assert "N/A" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert latency(list(range(100)))["p99_seconds"] == 98


def test_capacity_stop_and_sanitized_group_errors(tmp_path, monkeypatch):
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("app.evaluation.checkpoint.shutil.disk_usage", lambda _: usage(10, 9, 1))
    with pytest.raises(RuntimeError, match="low_disk_space"):
        check_capacity(tmp_path, tmp_path)
    (tmp_path / "STOP").touch()
    with pytest.raises(RuntimeError, match="stop_requested"):
        check_capacity(tmp_path, tmp_path)
    assert safe_error(ExceptionGroup("private", [ValueError("secret")])) == "ValueError"


async def test_ready_id_projections_preserve_scope_and_status(settings):
    from app.evaluation.vector_reuse import verification_chunk_ids

    services = Container(settings)
    try:
        await services.start()
        doc = await services.knowledge.ingest("example.txt", b"public text")
        assert services.repository.ready_document_ids() == [doc["id"]]
        assert services.repository.ready_chunk_ids() == [doc["id"] + "_0"]
        assert verification_chunk_ids(services.repository) == services.repository.ready_chunk_ids()
        token = knowledge_scope.set(("inaccessible",))
        try:
            assert services.repository.ready_document_ids() == []
            assert services.repository.ready_chunk_ids() == []
            assert verification_chunk_ids(services.repository) == []
        finally:
            knowledge_scope.reset(token)
        services.repository.fail_document(doc["id"])
        assert services.repository.ready_document_ids() == []
        assert services.repository.ready_chunk_ids() == []
        assert verification_chunk_ids(services.repository) == []
    finally:
        await services.close()
