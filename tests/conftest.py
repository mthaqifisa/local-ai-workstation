"""Shared fixtures. Every test runs with HOME, LAW_HOME and TMPDIR inside a throw-away directory,
and launchctl/brew/colima are blocked so no test can touch the real machine (v5 lesson)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REAL_RUN = subprocess.run
BLOCKED = {"launchctl", "brew", "colima", "docker", "omlx", "osascript", "shortcuts", "peekaboo"}


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker("e2e"):
        return tmp_path
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LAW_HOME", str(home / "LAW"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()

    def guarded(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        exe = os.path.basename(str(cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd).split()[0]))
        if exe in BLOCKED:
            raise AssertionError(f"test tried to run real {exe}: {cmd}")
        return REAL_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    return home
