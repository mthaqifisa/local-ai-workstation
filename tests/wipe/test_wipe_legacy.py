"""Tests for scripts/wipe_legacy.py against a fake HOME seeded with every inventory item."""

from __future__ import annotations

import importlib.util
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("wipe_legacy", REPO / "scripts" / "wipe_legacy.py")
assert spec and spec.loader
wl = importlib.util.module_from_spec(spec)
sys.modules["wipe_legacy"] = wl
spec.loader.exec_module(wl)

INVENTORY = tomllib.loads((REPO / "settings" / "legacy_inventory.toml").read_text())


class FakeOps(wl.SysOps):
    def __init__(self, brew_installed: list[str], brew_uses: dict[str, list[str]]) -> None:
        self.calls: list[list[str]] = []
        self.brew_installed = brew_installed
        self.brew_uses = brew_uses

    def run(self, cmd: list[str]) -> tuple[int, str]:
        self.calls.append(cmd)
        if cmd[:3] == ["brew", "list", "--formula"]:
            return 0, "\n".join(self.brew_installed)
        if cmd[:3] == ["brew", "uses", "--installed"]:
            return 0, "\n".join(self.brew_uses.get(cmd[3], []))
        return 0, ""

    def uid(self) -> int:
        return 501

    def which(self, name: str) -> str | None:
        return f"/opt/homebrew/bin/{name}"


def seed(home: Path) -> None:
    for block in INVENTORY["paths"]:
        if block["category"] == "keep":
            continue
        for rel in block["items"]:
            p = home / rel
            if p.suffix in {".env", ".json"} or rel.endswith((".env", ".json", ".law-backup")):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("X=1\n")
            else:
                (p / "sub").mkdir(parents=True, exist_ok=True)
                (p / "sub" / "f.bin").write_bytes(b"0" * 1024)
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True, exist_ok=True)
    for label in ["com.law.worker", "com.aiws.mlx", "ai.openclaw.gateway", "com.google.keystone.agent"]:
        (la / f"{label}.plist").write_text("<plist/>")
    (home / ".colima" / "default").mkdir(parents=True, exist_ok=True)
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ["claude", "law", "litellm", "mflux-generate", "mflux-save", "keepme"]:
        (bin_dir / name).write_text("#!/bin/sh\n")
    (bin_dir / "nano-pdf").symlink_to(home / "does-not-exist")
    (home / ".zshrc").write_text("# OpenClaw Completion\n[ -f x/openclaw.zsh ] && source x\nalias ll='ls -l'\n")
    (home / ".zprofile").write_text('eval "$(/opt/homebrew/bin/brew shellenv zsh)"\nexport PATH="$HOME/.local/bin:$PATH"\n')


@pytest.fixture()
def env(tmp_path: Path):
    home = tmp_path / "wipehome"
    tmpdir = tmp_path / "wipetmp"
    home.mkdir()
    tmpdir.mkdir()
    (tmpdir / "mlx-openai-prompt-cache-abc").mkdir()
    (tmpdir / "unrelated").mkdir()
    seed(home)
    ops = FakeOps(brew_installed=["socat", "pytorch", "openai-whisper", "codexbar", "git"],
                  brew_uses={"pytorch": ["openai-whisper"], "socat": ["somethingelse"]})
    return home, tmpdir, ops


def make(home: Path, tmpdir: Path, ops: FakeOps, apply: bool) -> wl.Wiper:
    return wl.Wiper(home, home / "LAW", INVENTORY, ops, tmpdir, apply, "test")


def snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_dry_run_changes_nothing(env):
    home, tmpdir, ops = env
    before = snapshot(home) | snapshot(tmpdir)
    report = make(home, tmpdir, ops, apply=False).run()
    assert snapshot(home) | snapshot(tmpdir) == before
    assert not [c for c in ops.calls if c[0] in {"launchctl", "colima"} or c[:2] == ["brew", "uninstall"]]
    assert any("[delete]" in a for a in report.actions)
    assert report.freed_bytes == 0


def test_apply_removes_inventory_and_keeps_the_rest(env):
    home, tmpdir, ops = env
    report = make(home, tmpdir, ops, apply=True).run()
    assert report.errors == []
    for block in INVENTORY["paths"]:
        for rel in block["items"]:
            exists = (home / rel).exists()
            if block["category"] == "keep":
                continue
            if block["category"] == "delete":
                assert not exists, rel
    bin_dir = home / ".local" / "bin"
    assert sorted(p.name for p in bin_dir.iterdir()) == ["claude", "keepme"]
    assert not (tmpdir / "mlx-openai-prompt-cache-abc").exists()
    assert (tmpdir / "unrelated").exists()
    la = home / "Library" / "LaunchAgents"
    assert sorted(p.name for p in la.iterdir()) == ["com.google.keystone.agent.plist"]
    assert ["launchctl", "bootout", "gui/501/com.law.worker"] in ops.calls
    assert report.freed_bytes > 0


def test_archive_holds_legacy_data_before_delete(env):
    home, tmpdir, ops = env
    make(home, tmpdir, ops, apply=True).run()
    tar_path = home / "LAW" / "archive" / "legacy-test" / "legacy.tar.gz"
    with tarfile.open(tar_path) as tar:
        names = set(tar.getnames())
    assert ".law/.env" in names
    assert "SourceCode/OpenSourceLocalAI/v4" in names
    assert oct(tar_path.stat().st_mode)[-3:] == "600"


def test_shell_rc_only_legacy_lines_removed(env):
    home, tmpdir, ops = env
    zprofile_before = (home / ".zprofile").read_text()
    make(home, tmpdir, ops, apply=True).run()
    assert (home / ".zshrc").read_text() == "alias ll='ls -l'\n"
    assert (home / ".zshrc.bak.law6").exists()
    assert (home / ".zprofile").read_text() == zprofile_before


def test_brew_respects_reverse_dependencies(env):
    home, tmpdir, ops = env
    make(home, tmpdir, ops, apply=True).run()
    uninstalled = [c[-1] for c in ops.calls if c[:2] == ["brew", "uninstall"]]
    # pytorch is only needed by openai-whisper, which is also being removed -> both go.
    assert sorted(uninstalled) == ["codexbar", "openai-whisper", "pytorch"]
    assert "socat" not in uninstalled  # still needed by another formula


def test_second_apply_is_a_noop(env):
    home, tmpdir, ops = env
    make(home, tmpdir, ops, apply=True).run()
    after_first = snapshot(home)
    ops.brew_installed = ["git", "socat"]
    report = make(home, tmpdir, ops, apply=True).run()
    assert report.freed_bytes == 0
    assert snapshot(home) == after_first


def test_guard_refuses_paths_outside_home(env, tmp_path):
    home, tmpdir, ops = env
    outside = tmp_path / "outside"
    outside.mkdir()
    inv = {"paths": [{"category": "delete", "items": ["../outside"]}]}
    w = wl.Wiper(home, home / "LAW", inv, ops, tmpdir, True, "t")
    w.home = home
    w.delete_paths()
    assert outside.exists()
