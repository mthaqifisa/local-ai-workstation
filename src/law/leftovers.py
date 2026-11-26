"""Processes an agent left running after its session (seen 2026-11-26: a ticket started its Java client with
`nohup java -jar ... &` to try it; the server kept running for 10 hours, listening on the network).

After every agent session LAW stops what that session left behind, and nothing else. A process is a leftover only
when all three hold: its working folder is inside the project, it started during the session, and it was detached
(its parent is launchd, pid 1, as `nohup ... &` leaves it). The owner's own terminals and apps keep their shell or
app as parent, and LAW's services run from LAW's folders, so neither can match.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psutil

log = logging.getLogger(__name__)
GRACE_S = 5.0  # time to exit after SIGTERM before SIGKILL


def find(workdir: Path, since: float) -> list[psutil.Process]:
    """Detached processes started at or after `since` (epoch seconds) whose working folder is inside `workdir`."""
    root = workdir.resolve()
    found = []
    for proc in psutil.process_iter(["pid", "ppid", "create_time"]):
        try:
            if proc.info["ppid"] != 1 or proc.info["create_time"] < since:
                continue
            cwd = Path(proc.cwd()).resolve()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
        if cwd == root or root in cwd.parents:
            found.append(proc)
    return found


def stop(workdir: Path, since: float) -> list[str]:
    """Stop the session's leftovers (SIGTERM, then SIGKILL after GRACE_S). Returns what was stopped, for the log."""
    procs = find(workdir, since)
    stopped = []
    for proc in procs:
        try:
            stopped.append(f"{proc.pid} {' '.join(proc.cmdline())[:120]}")
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(procs, timeout=GRACE_S)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for line in stopped:
        log.info("stopped a process the agent left running: %s", line)
    return stopped
