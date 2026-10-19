"""Every test runs against a throw-away home folder, so nothing a test does can touch the real
~/.law (undo histories, queue, secrets) or the real caches — and real state can't leak into a
test either. Tests that need a specific HOME still set their own (their monkeypatch runs later).

launchd is NOT isolated by HOME: a test that reached the real `launchctl bootout com.law.telegram`
(law configure --telegram-off) silently killed the developer's running bot on every test run.
So every state-changing launchctl call is answered by a fake here, whatever code path makes it."""
from __future__ import annotations

import subprocess

import pytest

LAUNCHCTL_WRITES = {"bootout", "bootstrap", "kickstart", "kill", "load", "unload", "enable", "disable",
                    "remove", "start", "stop"}
_real_run = subprocess.run


def _guarded_run(args, *a, **kw):
    argv = list(args) if isinstance(args, (list, tuple)) else str(args).split()
    if argv and str(argv[0]).endswith("launchctl") and len(argv) > 1 and argv[1] in LAUNCHCTL_WRITES:
        out = "" if kw.get("text") or kw.get("universal_newlines") else b""
        return subprocess.CompletedProcess(argv, 0, out, out)
    return _real_run(args, *a, **kw)


@pytest.fixture(autouse=True)
def _no_real_launchd(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _guarded_run)
    yield


ISOLATE = ("LAW_HOME", "HF_HOME", "HF_HUB_CACHE", "LAW_BIND_HOST", "LAW_TRUSTED_NETS", "LAW_ALLOWED_HOSTS")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_CACHE_DIR", str(home / ".cache" / "uv"))
    for k in ISOLATE:
        monkeypatch.delenv(k, raising=False)
    yield
