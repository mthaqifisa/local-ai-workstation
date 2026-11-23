"""Runtime pins: one source of truth in the registry, honoured by the installer and checked by doctor."""

from __future__ import annotations

import re
from pathlib import Path

from law import registry
from law.install.steps import Ctx, OpenCodePin, tool_version
from law.paths import LawPaths
from law.sysapi import Result, SysAPI

REPO = Path(__file__).resolve().parents[2]


class _Sys(SysAPI):
    def __init__(self, version_out: str) -> None:
        self.version_out, self.calls = version_out, []  # type: str, list[list[str]]

    def run(self, cmd, *, env=None, timeout=1800, cwd=None):  # type: ignore[no-untyped-def,override]
        self.calls.append(cmd)
        if cmd[-1] == "--version":
            return Result(0, self.version_out) if self.version_out else Result(127, "not found")
        return Result(0, "")


def _ctx(tmp_path: Path, version_out: str) -> Ctx:
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    return Ctx(paths, registry.load(paths.registry_file), _Sys(version_out))


def test_registry_pins_exact_versions() -> None:
    reg = registry.load(REPO / "settings" / "registry.yaml")
    assert reg.pins is not None
    for v in (reg.pins.opencode, reg.pins.omlx):
        assert re.fullmatch(r"\d+\.\d+\.\d+", v), v
    assert reg.pins.opencode.startswith("1."), "OpenCode 2 only after its gates pass (ADR-0024)"


def test_brewfile_does_not_install_an_unpinned_opencode() -> None:
    lines = [ln.strip() for ln in (REPO / "Brewfile").read_text().splitlines()]
    assert 'brew "opencode"' not in lines


def test_tool_version_parses_real_outputs() -> None:
    assert tool_version("1.18.7\n") == "1.18.7"
    assert tool_version("omlx 0.8.0") == "0.8.0"
    assert tool_version("0.7.1rc1") == "0.7.1rc1"
    assert tool_version("command not found") == ""


def test_opencode_pin_repairs_a_mismatch(tmp_path: Path) -> None:
    c = _ctx(tmp_path, "1.19.6")
    step = OpenCodePin(c)
    want = c.reg.pins.opencode  # type: ignore[union-attr]
    assert step.plan() == [f"opencode 1.19.6 -> {want}"]
    step.apply()
    assert any(cmd[:2] == ["npm", "install"] and f"opencode-ai@{want}" in cmd for cmd in c.sys.calls)  # type: ignore[attr-defined]
    assert step.link.is_symlink() and step.link.readlink().name == "opencode"


def test_opencode_pin_is_quiet_when_it_matches(tmp_path: Path) -> None:
    c = _ctx(tmp_path, "")
    c.sys.version_out = c.reg.pins.opencode  # type: ignore[attr-defined,union-attr]
    assert OpenCodePin(c).plan() == []


def test_rendered_opencode_config_never_autoupdates() -> None:
    src = (REPO / "src" / "law" / "install" / "steps.py").read_text()
    assert '"autoupdate": False' in src
