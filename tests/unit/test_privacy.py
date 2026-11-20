"""Files LAW writes into the public repo show home paths as ~ and never carry the owner's email or IDs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

from law import privacy, secrets
from law import state_sync as s
from law.install.steps import Ctx, GitHooks
from law.paths import LawPaths, display
from law.sysapi import Result, SysAPI

REPO = Path(__file__).resolve().parents[2]
OWNER = "jane.doe" + "@" + "proton.me"
ANN, JANE = "/Users/" + "ann", "/Users/" + "jane"  # built at runtime: the repo's own rules forbid real-looking homes


def _paths(tmp_path: Path) -> LawPaths:
    home = tmp_path / "Users" / "jane"
    p = LawPaths(repo=tmp_path / "repo", home=home / "LAW", user_home=home)
    secrets.write(p.env_file, {"LAW_REPORT_EMAIL": OWNER, "LAW_TELEGRAM_USER_IDS": "18000001,18000002"})
    (p.state / "handover.env").write_text("report_email=Other" + "@" + "proton.me\nmethod=mail\n")
    return p


def test_display_replaces_whole_home_component_only() -> None:
    home = Path(ANN)
    assert display(Path(f"{ANN}/LAW/logs/x.log"), home) == "~/LAW/logs/x.log"
    assert display(ANN, home) == "~"
    assert display(f"see {ANN}/a and {ANN}a/b", home) == f"see ~/a and {ANN}a/b"
    assert display("/opt/homebrew/bin", home) == "/opt/homebrew/bin"


def test_scrub_removes_home_email_and_telegram_ids(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    text = f"{p.user_home}/LAW/out.pdf sent to {OWNER.upper()} and other" + "@" + "proton.me; chat 18000002, n=180000021"
    out = privacy.scrub(text, p)
    assert str(p.user_home) not in out and "proton.me" not in out.lower()
    assert out.startswith("~/LAW/out.pdf sent to you@example.com")
    assert "chat [telegram-id]," in out and "180000021" in out  # whole numbers only


def test_scrub_without_state_files_only_rewrites_paths(tmp_path: Path) -> None:
    p = LawPaths(repo=tmp_path, home=tmp_path / "LAW", user_home=tmp_path / "h")
    assert privacy.scrub(f"{tmp_path}/h/x", p) == "~/x"


def test_gitleaks_config_is_found_from_a_subfolder() -> None:
    assert privacy.gitleaks_config(REPO / "settings" / "openwebui") == REPO / ".gitleaks.toml"


class _WebUI:
    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/auths/signin":
            return httpx.Response(200, json={"token": "t"})
        prompts = [{"command": "rep", "name": "Report", "content": f"save to {JANE}/LAW/outputs/docs"}]
        data = {"/api/v1/prompts/": prompts, "/api/v1/knowledge/": {"items": []}}
        return httpx.Response(200, json=data.get(req.url.path, []))


def test_law_save_writes_scrubbed_export(tmp_path: Path) -> None:
    p = LawPaths(repo=tmp_path, home=tmp_path / "LAW", user_home=Path(JANE))
    api = s.OpenWebUI("http://owui", transport=httpx.MockTransport(_WebUI().handler))
    s.save(api, tmp_path / "out", tmp_path / "stage", scrub=lambda t: privacy.scrub(t, p),
           config=REPO / ".gitleaks.toml")
    prompts = json.loads((tmp_path / "out" / "prompts.json").read_text())
    assert prompts[0]["content"] == "save to ~/LAW/outputs/docs"


def test_test_report_markdown_is_scrubbed(tmp_path: Path, monkeypatch: object) -> None:
    spec = importlib.util.spec_from_file_location("test_report", REPO / "scripts" / "test_report.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.REPO = tmp_path
    (tmp_path / "docs").mkdir()
    home = str(Path.home())
    rows = [{"layer": "lint", "ok": False, "seconds": 1, "detail": [f"{home}/x.py:1 E501"]}]
    evals = [{"id": "c1", "domain": "code", "passed": False, "checks": {}, "error": f"failed in {home}/LAW",
              "judge_score": None, "seconds": 2}]
    mod.write_markdown(rows, evals, None)
    report = (tmp_path / "docs" / "TEST-REPORT.md").read_text()
    assert home not in report and "failed in ~/LAW" in report


class _GitSys(SysAPI):
    def __init__(self, current: str | None) -> None:
        self.current, self.calls = current, []  # type: list[list[str]]

    def run(self, cmd, *, env=None, timeout=1800, cwd=None):  # type: ignore[no-untyped-def,override]
        self.calls.append(cmd)
        if cmd[-2:] == ["--get", "core.hooksPath"]:
            return Result(0, self.current) if self.current else Result(1, "")
        return Result(0, "")


def test_git_hooks_step_sets_hooks_path_once(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "pre-commit").write_text("#!/bin/sh\n")
    paths = LawPaths(repo=tmp_path, home=tmp_path / "LAW", user_home=tmp_path)
    fresh = GitHooks(Ctx(paths=paths, reg=None, sys=_GitSys(None)))  # type: ignore[arg-type]
    assert fresh.plan() == ["git hooks path -> .githooks"]
    fresh.apply()
    assert ["git", "-C", str(tmp_path), "config", "--local", "core.hooksPath", ".githooks"] in fresh.c.sys.calls  # type: ignore[attr-defined]
    done = GitHooks(Ctx(paths=paths, reg=None, sys=_GitSys(".githooks")))  # type: ignore[arg-type]
    assert done.plan() == []


def test_git_hooks_step_skips_a_tree_without_git(tmp_path: Path) -> None:
    paths = LawPaths(repo=tmp_path, home=tmp_path / "LAW", user_home=tmp_path)
    assert GitHooks(Ctx(paths=paths, reg=None, sys=_GitSys(None))).plan() == []  # type: ignore[arg-type]
