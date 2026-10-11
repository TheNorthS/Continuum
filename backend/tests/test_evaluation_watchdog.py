from types import SimpleNamespace
from unittest.mock import Mock

from app.evaluation import watchdog


def test_heartbeat_does_not_hide_stall_but_stage_or_count_resets_budget():
    now = [0]
    guard = watchdog.ProgressWatchdog(timeout=600, clock=lambda: now[0])
    assert not guard.expired({"stage": "query", "done": 271, "updated_at": "old"})
    now[0] = 599
    assert not guard.expired({"stage": "query", "done": 271, "updated_at": "new"})
    now[0] = 600
    assert guard.expired({"stage": "query", "done": 271, "updated_at": "newer"})
    assert not guard.expired({"stage": "query", "done": 272})
    now[0] = 1000
    assert not guard.expired({"stage": "verifying_index"})
    now[0] = 1600
    assert guard.expired({"stage": "verifying_index"})


def test_windows_kills_only_owned_child_tree_and_waits(monkeypatch):
    monkeypatch.setattr(watchdog.os, "name", "nt")
    run = Mock()
    monkeypatch.setattr(watchdog.subprocess, "run", run)
    child = SimpleNamespace(pid=12345, poll=lambda: None, wait=Mock())
    watchdog.terminate_owned_worker(child)
    run.assert_called_once_with(
        ["taskkill", "/PID", "12345", "/T", "/F"], check=True, capture_output=True, timeout=30
    )
    child.wait.assert_called_once_with(timeout=30)


def test_finished_process_is_never_killed(monkeypatch):
    run = Mock()
    monkeypatch.setattr(watchdog.subprocess, "run", run)
    watchdog.terminate_owned_worker(SimpleNamespace(poll=lambda: 0))
    run.assert_not_called()
