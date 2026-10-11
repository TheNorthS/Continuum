"""Detect a stalled owned worker even when its event loop or SQL thread hangs."""

import os
import signal
import subprocess
import time


class ProgressWatchdog:
    def __init__(self, timeout=600, clock=time.monotonic):
        self.clock, self.timeout = clock, timeout
        self.last_progress = clock()
        self.watermark = None

    def expired(self, state):
        # Supervisor timestamps and API activity are not completed work. Only
        # actual stage/count advances keep the worker alive.
        watermark = (state.get("stage"), state.get("documents"), state.get("done"))
        now = self.clock()
        if watermark != self.watermark:
            self.watermark = watermark
            self.last_progress = now
        return now - self.last_progress >= self.timeout


def terminate_owned_worker(child):
    if child.poll() is not None:
        return
    if os.name == "nt":
        # The venv executable is a launcher on Windows: terminating just that
        # PID leaves its actual Python child alive and the run lock held.
        subprocess.run(
            ["taskkill", "/PID", str(child.pid), "/T", "/F"], check=True, capture_output=True, timeout=30
        )
    else:
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=30)
