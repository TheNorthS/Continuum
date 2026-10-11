import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

from app.evaluation import suite
from app.evaluation.checkpoint import Checkpoint, safe_error


def test_supervisor_keeps_retrying_progress_and_publishes_only_completed_runs(tmp_path, monkeypatch):
    plan = {
        "suite_name": "test-suite",
        "runs": {"t2": {"run_name": "t2-test", "full": False, "questions": 1, "documents": 5, "seed": 42}},
    }
    run = tmp_path / "test-results/evaluation/t2-test"
    attempts = []

    def child(*args, **kwargs):
        attempts.append(1)
        checkpoint = Checkpoint(run)
        checkpoint.put("documents", str(len(attempts)), "document")
        checkpoint.close()
        (run / "progress.json").write_text(json.dumps({"stage": "failed", "error": "upstream_unavailable"}))
        code = 0 if len(attempts) == 5 else 1
        return SimpleNamespace(returncode=code, poll=lambda: code)

    monkeypatch.setattr(suite, "ROOT", tmp_path)
    monkeypatch.setattr(suite.subprocess, "Popen", child)
    monkeypatch.setattr(suite.time, "sleep", Mock())
    publish = Mock()
    monkeypatch.setattr(suite, "publish", publish)
    assert suite.run_suite(tmp_path / "runtime", plan) == 0
    assert len(attempts) == 5
    assert suite.committed_counts(run) == {"documents": 5}
    publish.assert_called_once_with(plan=plan)


def test_supervisor_stops_after_three_no_progress_failures(tmp_path, monkeypatch):
    plan = {
        "suite_name": "test-suite",
        "runs": {"t2": {"run_name": "t2-test", "full": False, "questions": 1, "documents": 5, "seed": 42}},
    }
    run = tmp_path / "test-results/evaluation/t2-test"
    attempts = []

    def child(*args, **kwargs):
        attempts.append(1)
        (run / "progress.json").write_text(json.dumps({"stage": "failed", "error": "upstream_unavailable"}))
        return SimpleNamespace(returncode=1, poll=lambda: 1)

    monkeypatch.setattr(suite, "ROOT", tmp_path)
    monkeypatch.setattr(suite.subprocess, "Popen", child)
    monkeypatch.setattr(suite.time, "sleep", Mock())
    publish = Mock()
    monkeypatch.setattr(suite, "publish", publish)
    assert suite.run_suite(tmp_path / "runtime", plan) == 1
    assert len(attempts) == 3
    state = json.loads((run.parent / "test-suite/progress.json").read_text())
    assert state["consecutive_stalled_attempts"] == 3
    publish.assert_not_called()


def test_sqlite_codes_are_sanitized_and_only_busy_is_retryable():
    from sqlalchemy.exc import OperationalError

    for code, expected in (
        (sqlite3.SQLITE_BUSY, "sqlite_busy"),
        (sqlite3.SQLITE_FULL, "sqlite_full"),
        (sqlite3.SQLITE_CORRUPT, "sqlite_corrupt"),
    ):
        original = sqlite3.OperationalError("private statement")
        original.sqlite_errorcode = code
        error = OperationalError("private SQL", {"key": "private"}, original)
        assert safe_error(error) == expected


def test_verified_completed_run_is_reused_without_subprocess(tmp_path, monkeypatch):
    scope = {"run_name": "done", "documents": 1, "questions": 1, "full": False, "seed": 42}
    plan = {"suite_name": "suite", "runs": {"t2": scope}}
    folder = tmp_path / "test-results/evaluation/done"
    folder.mkdir(parents=True)
    checkpoint = Checkpoint(folder)
    checkpoint.put("documents", "d", "document")
    checkpoint.put("results", "q", {})
    checkpoint.close()
    (folder / "metrics.json").write_text(json.dumps({"status": "complete"}))
    monkeypatch.setattr(suite, "ROOT", tmp_path)
    validation, pin, publish = Mock(), Mock(), Mock()
    monkeypatch.setattr(suite, "validated_results", validation)
    monkeypatch.setattr(suite, "pin_implementation", pin)
    monkeypatch.setattr(suite, "publish", publish)
    launch = Mock(side_effect=AssertionError("Completed run should not restart"))
    monkeypatch.setattr(suite.subprocess, "Popen", launch)
    assert suite.run_suite(tmp_path / "runtime", plan) == 0
    validation.assert_called_once_with(tmp_path, {"runs": {"t2": scope}})
    pin.assert_called_once_with(folder)
    launch.assert_not_called()


def test_watchdog_retries_live_but_stalled_workers_and_preserves_checkpoint(tmp_path, monkeypatch):
    plan = {
        "suite_name": "stall-suite",
        "runs": {"t2": {"run_name": "stalled", "documents": 2, "questions": 2, "full": False, "seed": 42}},
    }
    directory = tmp_path / "test-results/evaluation/stalled"
    directory.mkdir(parents=True)
    checkpoint = Checkpoint(directory)
    checkpoint.put("results", "completed", {"answer": "kept"})
    checkpoint.close()
    (directory / "progress.json").write_text(json.dumps({"stage": "query", "done": 1}))
    created = []

    def launch(*args, **kwargs):
        child = SimpleNamespace(pid=999, returncode=None)
        child.poll = lambda: child.returncode
        created.append(child)
        return child

    def kill(child):
        child.returncode = 1

    monkeypatch.setattr(suite, "ROOT", tmp_path)
    monkeypatch.setattr(suite.subprocess, "Popen", launch)
    monkeypatch.setattr(
        suite, "ProgressWatchdog", lambda: SimpleNamespace(timeout=600, expired=lambda _: True)
    )
    monkeypatch.setattr(suite, "terminate_owned_worker", kill)
    monkeypatch.setattr(suite.time, "sleep", Mock())
    publish = Mock()
    monkeypatch.setattr(suite, "publish", publish)
    assert suite.run_suite(tmp_path / "runtime", plan) == 1
    assert len(created) == 3
    checkpoint = Checkpoint(directory)
    assert checkpoint.read("results") == {"completed": {"answer": "kept"}}
    checkpoint.close()
    publish.assert_not_called()
