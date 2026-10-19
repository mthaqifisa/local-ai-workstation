"""Storage reclaim, uninstall and secrets commands against a sandboxed HOME."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from rich.console import Console

from law import ops, registry, secrets
from law.paths import LawPaths
from law.sysapi import Result, SysAPI

REPO = Path(__file__).resolve().parents[2]


class FakeSys(SysAPI):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded: set[str] = set()

    def run(self, cmd, *, env=None, timeout=1800, cwd=None):  # type: ignore[no-untyped-def,override]
        self.calls.append(cmd)
        return Result(0, "")

    def launchd_loaded(self, label: str) -> bool:
        return label in self.loaded

    def launchd_bootout(self, label: str) -> Result:
        self.loaded.discard(label)
        return Result(0, "")


@pytest.fixture()
def paths(sandbox: Path) -> LawPaths:
    p = LawPaths(repo=REPO, home=sandbox / "LAW", user_home=sandbox)
    for d in p.skeleton():
        d.mkdir(parents=True, exist_ok=True)
    return p


def _blob(path: Path, size: int = 1024) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "w.safetensors").write_bytes(b"0" * size)


def test_reclaim_removes_only_unregistered_model_caches(paths: LawPaths):
    hub = paths.models / "hf" / "hub"
    keep = registry.load(paths.registry_file).hf_repos_in_use()
    for repo in keep:
        _blob(hub / f"models--{repo.replace('/', '--')}")
    _blob(hub / "models--mlx-community--whisper-large-v3-turbo")  # retired build
    freed = ops.reclaim(paths, FakeSys(), Console(quiet=True))
    left = {d.name for d in hub.iterdir()}
    assert "models--mlx-community--whisper-large-v3-turbo" not in left
    assert all(f"models--{r.replace('/', '--')}" in left for r in keep)
    assert freed >= 1024


def test_reclaim_dry_run_deletes_nothing(paths: LawPaths):
    victim = paths.models / "hf" / "hub" / "models--someone--old"
    _blob(victim)
    ops.reclaim(paths, FakeSys(), Console(quiet=True), dry_run=True)
    assert victim.exists()


def test_reclaim_ages_out_trash_but_keeps_recent(paths: LawPaths):
    old, new = paths.trash / "old-item", paths.trash / "new-item"
    _blob(old)
    _blob(new)
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    ops.reclaim(paths, FakeSys(), Console(quiet=True))
    assert not old.exists() and new.exists()


def test_uninstall_removes_services_and_keeps_data(paths: LawPaths):
    paths.launch_agents.mkdir(parents=True, exist_ok=True)
    for label in ("com.law6.api", "com.law6.worker"):
        (paths.launch_agents / f"{label}.plist").write_text("<plist/>")
    (paths.launch_agents / "com.google.keystone.agent.plist").write_text("<plist/>")
    zp = paths.user_home / ".zprofile"
    zp.write_text("alias x=y\n# >>> LAW v6 >>>\nsource env\n# <<< LAW v6 <<<\n")
    sysapi = FakeSys()
    sysapi.loaded = {"com.law6.api", "com.law6.worker"}
    ops.uninstall(paths, sysapi, Console(quiet=True), system=True)
    assert sorted(p.name for p in paths.launch_agents.iterdir()) == ["com.google.keystone.agent.plist"]
    assert zp.read_text() == "alias x=y\n"
    assert paths.models.exists()


def test_secrets_set_rejects_unknown_keys_and_export_omits_generated(paths: LawPaths, tmp_path: Path):
    secrets.ensure(paths.env_file)
    with pytest.raises(ValueError):
        ops.secrets_set(paths, "WEBUI_SECRET_KEY", "x")
    ops.secrets_set(paths, "LAW_TELEGRAM_TOKEN", "123:abc")
    out = tmp_path / "mine.env"
    ops.secrets_export(paths, out)
    exported = secrets.read(out)
    assert exported == {"LAW_TELEGRAM_TOKEN": "123:abc"}
    assert oct(out.stat().st_mode)[-3:] == "600"


def test_reclaim_truncates_huge_live_logs_but_never_deletes_them(paths: LawPaths):
    log = paths.logs / "worker.log"
    log.write_bytes(b"x" * 1000 + b"TAIL")
    past = time.time() - 60 * 86400
    os.utime(log, (past, past))
    ops.reclaim(paths, FakeSys(), Console(quiet=True))
    assert log.exists()  # old but live: kept
    big = paths.logs / "omlx.log"
    with big.open("wb") as fh:
        fh.truncate(210_000_000)
        fh.seek(210_000_000 - 4)
        fh.write(b"LAST")
    ops.reclaim(paths, FakeSys(), Console(quiet=True))
    assert big.stat().st_size == 10_000_000 and big.read_bytes().endswith(b"LAST")


def test_reclaim_removes_stale_partial_downloads_only(paths: LawPaths):
    blobs = paths.models / "hf" / "hub" / "models--Qwen--Qwen-Image-2.1" / "blobs"
    blobs.mkdir(parents=True)
    stale, active = blobs / "aaa.incomplete", blobs / "bbb.incomplete"
    stale.write_bytes(b"0" * 100)
    active.write_bytes(b"0" * 100)
    past = time.time() - 2 * 3600
    os.utime(stale, (past, past))
    ops.reclaim(paths, FakeSys(), Console(quiet=True))
    assert not stale.exists() and active.exists()


def test_reclaim_removes_retired_llm_folders_only_when_complete(paths: LawPaths):
    retired = paths.llm_models / "old-model-6bit"
    downloading = paths.llm_models / "brand-new-model"
    current = paths.llm_models / "qwen3.8-27b-8bit"
    for d in (retired, downloading, current):
        _blob(d)
    (retired / ".law-complete").write_text("rev\n")
    (current / ".law-complete").write_text("rev\n")
    ops.reclaim(paths, FakeSys(), Console(quiet=True))
    assert not retired.exists()
    assert current.exists() and downloading.exists()  # never touch a download in progress
