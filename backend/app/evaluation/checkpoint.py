"""Transactional progress for long-running evaluations, without quadratic JSON rewrites."""

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime


class RunLock:
    """OS-held lock; released automatically on crash, including on Windows."""

    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "run.lock"

    def __enter__(self):
        self.handle = self.path.open("a+b")
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("evaluation_already_running") from None
        return self

    def __exit__(self, *_):
        self.handle.close()


def safe_error(exc):
    from app.common.errors import AppError

    if isinstance(exc, BaseExceptionGroup):
        return safe_error(exc.exceptions[0])
    if isinstance(exc, AppError):
        return exc.code
    original = getattr(exc, "orig", exc)
    sqlite_code = getattr(original, "sqlite_errorcode", None)
    if isinstance(original, sqlite3.Error) and isinstance(sqlite_code, int):
        # Extended codes share the primary code in the low byte. Never include
        # SQL statements, parameters or raw exception text in progress logs.
        return {
            sqlite3.SQLITE_BUSY: "sqlite_busy",
            sqlite3.SQLITE_LOCKED: "sqlite_locked",
            sqlite3.SQLITE_FULL: "sqlite_full",
            sqlite3.SQLITE_IOERR: "sqlite_io_error",
            sqlite3.SQLITE_CORRUPT: "sqlite_corrupt",
        }.get(sqlite_code & 255, "sqlite_error_" + str(sqlite_code))
    if isinstance(exc, RuntimeError) and str(exc) in {
        "evaluation_stop_requested",
        "evaluation_low_disk_space",
        "evaluation_already_running",
        "evaluation_dependency_unavailable",
    }:
        return str(exc)
    return type(exc).__name__


def safe_error_frames(exc):
    """Only code locations, never exception text, source lines or frame locals."""
    from pathlib import Path

    if isinstance(exc, BaseExceptionGroup):
        return safe_error_frames(exc.exceptions[0])
    root = Path(__file__).parents[1]
    frames = []
    trace = exc.__traceback__
    while trace:
        path = Path(trace.tb_frame.f_code.co_filename)
        if path.is_relative_to(root):
            frames.append(
                {
                    "file": path.relative_to(root).as_posix(),
                    "line": trace.tb_lineno,
                    "function": trace.tb_frame.f_code.co_name,
                }
            )
        trace = trace.tb_next
    return frames


class Checkpoint:
    def __init__(self, directory):
        self.db = sqlite3.connect(directory / "checkpoint.db")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS progress (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))"
        )
        for kind, name in (("documents", "document-map.json"), ("results", "results.json")):
            if not self.db.execute("SELECT 1 FROM progress WHERE kind=? LIMIT 1", (kind,)).fetchone():
                path = directory / name
                if path.exists():
                    with self.db:
                        self.db.executemany(
                            "INSERT INTO progress VALUES (?,?,?)",
                            [
                                (kind, key, json.dumps(value, ensure_ascii=False))
                                for key, value in json.loads(path.read_text(encoding="utf-8")).items()
                            ],
                        )

    def read(self, kind):
        return {
            key: json.loads(value)
            for key, value in self.db.execute("SELECT id,value FROM progress WHERE kind=?", (kind,))
        }

    def put(self, kind, key, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO progress VALUES (?,?,?)",
                (kind, key, json.dumps(value, ensure_ascii=False)),
            )

    def close(self):
        self.db.close()


def check_capacity(directory, runtime):
    if (directory / "STOP").exists():
        raise RuntimeError("evaluation_stop_requested")
    # Check both host volumes; Milvus lives on the separately managed Docker disk.
    for path, reserve in ((directory, 1.5 * 1024**3), (runtime, 3 * 1024**3)):
        if shutil.disk_usage(path).free < reserve:
            raise RuntimeError("evaluation_low_disk_space")


def progress(directory, stage, **values):
    payload = {"stage": stage, "updated_at": datetime.now(UTC).isoformat(), **values}
    path = directory / "progress.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    print(json.dumps(payload, ensure_ascii=False), flush=True)
