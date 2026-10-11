"""Durable sequential benchmark; publish charts/README only after the planned runs finish."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

from app.config import ROOT
from app.evaluation.checkpoint import RunLock, progress, safe_error
from app.evaluation.implementation import pin_implementation
from app.evaluation.plan import DEFAULT_PLAN, load_plan
from app.evaluation.publish import publish, validated_results
from app.evaluation.watchdog import ProgressWatchdog, terminate_owned_worker


def committed_counts(directory):
    """Read only durable progress; timestamps and API calls do not count as work."""
    path = directory / "checkpoint.db"
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return dict(db.execute("SELECT kind,count(*) FROM progress GROUP BY kind"))


def stalled_attempts(previous, current, failures):
    advanced = any(current.get(kind, 0) > previous.get(kind, 0) for kind in ("documents", "results"))
    return 0 if advanced else failures + 1


def completed_run_verified(directory, dataset, scope):
    if not (directory / "metrics.json").exists():
        return False
    metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    if metrics.get("status") != "complete":
        return False
    counts = committed_counts(directory)
    if counts.get("documents") != scope["documents"] or counts.get("results") != scope["questions"]:
        raise ValueError("Completed evaluation checkpoint differs from its plan")
    # Retain completed observations; later outages must not cause re-scoring or
    # require loading that run's vector collection merely to resume the suite.
    validated_results(ROOT, {"runs": {dataset: scope}})
    pin_implementation(directory)
    return True


def run_suite(runtime_root, plan, query_concurrency=1):
    directory = ROOT / "test-results/evaluation" / plan["suite_name"]
    with RunLock(directory):
        snapshot = directory / "plan.json"
        if snapshot.exists() and json.loads(snapshot.read_text(encoding="utf-8")) != plan:
            raise ValueError("Evaluation scope changed; choose a new suite name")
        snapshot.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        for dataset, scope in plan["runs"].items():
            name = scope["run_name"]
            run_dir = directory.parent / name
            run_dir.mkdir(parents=True, exist_ok=True)
            if completed_run_verified(run_dir, dataset, scope):
                progress(directory, "reusing_completed_run", run=name, done=scope["questions"])
                continue
            attempt, stalled = 0, 0
            while True:
                attempt += 1
                before = committed_counts(run_dir)
                if (directory / "STOP").exists():
                    progress(directory, "stopped", run=name)
                    return 2
                command = [
                    sys.executable,
                    "-u",
                    "-m",
                    "app.evaluation.run",
                    "--dataset",
                    dataset,
                    "--run-name",
                    name,
                    "--runtime-root",
                    str(runtime_root),
                    "--query-concurrency",
                    str(query_concurrency),
                ]
                command += (
                    ["--full"]
                    if scope["full"]
                    else [
                        "--questions",
                        str(scope["questions"]),
                        "--documents",
                        str(scope["documents"]),
                        "--seed",
                        str(scope["seed"]),
                        "--exact-documents",
                        str(scope["documents"]),
                    ]
                )
                if scope.get("reuse_matching_vectors_from"):
                    command += ["--reuse-matching-vectors-from", scope["reuse_matching_vectors_from"]]
                progress(directory, "running", run=name, attempt=attempt)
                with (run_dir / "console.log").open("a", encoding="utf-8") as log:
                    child = subprocess.Popen(
                        command,
                        cwd=ROOT / "backend",
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        start_new_session=os.name != "nt",
                    )
                    watchdog = ProgressWatchdog()
                    stalled_state = None
                    try:
                        while child.poll() is None:
                            if (directory / "STOP").exists():
                                (run_dir / "STOP").touch()
                            state_file = run_dir / "progress.json"
                            state = (
                                json.loads(state_file.read_text(encoding="utf-8"))
                                if state_file.exists()
                                else {}
                            )
                            if watchdog.expired(state):
                                progress(
                                    directory,
                                    "restarting_stalled_worker",
                                    run=name,
                                    attempt=attempt,
                                    child=state,
                                    no_progress_seconds=watchdog.timeout,
                                )
                                terminate_owned_worker(child)
                                stalled_state = {
                                    "stage": "failed",
                                    "error": "evaluation_stalled",
                                    "previous_stage": state.get("stage"),
                                }
                                break
                            progress(
                                directory, "running", run=name, attempt=attempt, pid=child.pid, child=state
                            )
                            time.sleep(30)
                    except BaseException:
                        (run_dir / "STOP").touch()
                        raise
                if child.returncode == 0:
                    break
                state = stalled_state or json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
                if state.get("error") == "evaluation_stop_requested":
                    progress(directory, "stopped", run=name, child=state)
                    return 2
                transient = state.get("error") in {
                    "upstream_unavailable",
                    "upstream_error",
                    "model_unavailable",
                    "TimeoutError",
                    "ConnectError",
                    "ReadTimeout",
                    "RemoteProtocolError",
                    "MilvusException",
                    "sqlite_busy",
                    "sqlite_locked",
                    "evaluation_stalled",
                }
                committed = committed_counts(run_dir)
                stalled = stalled_attempts(before, committed, stalled)
                if not transient or stalled >= 3:
                    progress(
                        directory,
                        "failed",
                        run=name,
                        attempt=attempt,
                        child=state,
                        committed=committed,
                        consecutive_stalled_attempts=stalled,
                        note="Checkpoint retained; README results publication withheld",
                    )
                    return 1
                delay = 30 * 2**stalled
                progress(
                    directory,
                    "retry_wait",
                    run=name,
                    attempt=attempt,
                    child=state,
                    committed=committed,
                    consecutive_stalled_attempts=stalled,
                    retry_in_seconds=delay,
                )
                # Keep STOP responsive during backoff. Retry budget resets only
                # when the previous process committed new documents or answers.
                for _ in range(delay // 5):
                    if (directory / "STOP").exists():
                        progress(directory, "stopped", run=name, committed=committed)
                        return 2
                    time.sleep(5)
        progress(directory, "publishing")
        publish(plan=plan)
        progress(directory, "complete", readme=str(ROOT / "README.md"))
        return 0


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--query-concurrency", type=int, choices=range(1, 5), default=1)
    args = parser.parse_args()
    plan = load_plan(args.plan)
    try:
        code = run_suite(args.runtime_root.resolve(), plan, args.query_concurrency)
    except Exception as exc:
        directory = ROOT / "test-results/evaluation" / plan["suite_name"]
        if safe_error(exc) != "evaluation_already_running":
            progress(directory, "failed", error=safe_error(exc))
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
