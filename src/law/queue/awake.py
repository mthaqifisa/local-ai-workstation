"""Keep the Mac awake while jobs run.

macOS idle-sleeps a laptop on battery after a minute or two; an agent step then freezes mid-request and
resumes only when someone wakes the machine (seen 2026-10-21: a coder step paused 31 minutes). While any
job runs, the worker holds a `caffeinate` assertion: -i prevents idle sleep (on battery too), -s prevents
system sleep on AC power, and -w ties it to the worker so a crashed worker never leaves the Mac awake.
Closing the lid on battery still sleeps the Mac (macOS allows that only with sudo pmset); see docs/RUNBOOK.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence


def caffeinate_argv() -> list[str] | None:
    """The assertion command for this process, or None where `caffeinate` does not exist (not macOS)."""
    tool = shutil.which("caffeinate")
    return [tool, "-i", "-s", "-w", str(os.getpid())] if tool else None


class KeepAwake:
    """Holds a sleep assertion while the worker is busy. `update` is cheap and idempotent."""

    def __init__(self, argv: Sequence[str] | None) -> None:
        self.argv = list(argv) if argv else None
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def holding(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def update(self, busy: bool) -> None:
        if busy and self.argv and not self.holding:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        elif not busy and self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc = None
