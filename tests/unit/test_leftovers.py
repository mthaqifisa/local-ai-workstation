"""LAW stops what an agent session left running in its project, and nothing else (law.leftovers)."""

from __future__ import annotations

import contextlib
import subprocess
import time
from pathlib import Path

import psutil

from law import leftovers


def _detached(folder: Path, tag: str) -> int:
    """Start `sleep` the way an agent does with `nohup ... &`: orphaned, so launchd (pid 1) becomes its parent."""
    marker = folder / f"{tag}.pid"
    subprocess.run(["sh", "-c", f"cd '{folder}' && (nohup sleep 300 >/dev/null 2>&1 & echo $! > '{marker}')"],
                   check=True)
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            return int(marker.read_text())
        time.sleep(0.05)
    raise AssertionError("the detached process did not start")


def test_only_the_sessions_detached_processes_in_the_project_are_stopped(tmp_path: Path) -> None:
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    (project / "code").mkdir(parents=True)
    elsewhere.mkdir()
    before = _detached(project, "before")  # already running when the session began: not the session's
    time.sleep(1.1)
    began = time.time() - 0.5
    left = _detached(project / "code", "left")  # what the session left behind
    other = _detached(elsewhere, "other")  # detached, but not in this project
    child = subprocess.Popen(["sleep", "300"], cwd=project)  # still has a parent: not detached
    try:
        assert [p.pid for p in leftovers.find(project, began)] == [left]
        stopped = leftovers.stop(project, began)
        assert len(stopped) == 1 and stopped[0].startswith(f"{left} ")
        assert not psutil.pid_exists(left) or psutil.Process(left).status() == psutil.STATUS_ZOMBIE
        for pid in (before, other, child.pid):
            assert psutil.pid_exists(pid)
    finally:
        child.kill()
        for pid in (before, other, left):
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(pid).kill()


def test_nothing_to_stop_is_quiet(tmp_path: Path) -> None:
    assert leftovers.stop(tmp_path, time.time()) == []
