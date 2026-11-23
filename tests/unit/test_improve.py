"""law.improve: proposals are built on a branch in a worktree and reach the repo only when applied."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from law.improve import BUILD_PROMPT, Improvements, build_payload, message
from law.paths import LawPaths


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def paths(tmp_path: Path) -> LawPaths:
    repo = tmp_path / "SourceCode" / "v6"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "bot@example.com")
    git(repo, "config", "user.name", "bot")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    return LawPaths(repo=repo, home=tmp_path / "LAW", user_home=tmp_path)


def test_build_then_apply_merges_the_branch(paths: LawPaths) -> None:
    imp = Improvements(paths)
    p = imp.add("Tony cannot send an existing file", "the memo was asked for twice", "add a send_file tool", "tony")
    assert imp.add("tony cannot send an existing file", "", "x", "tony").id == p.id  # one open proposal per problem
    wt = imp.prepare(p)
    assert wt.parent.name == "v6-improve" and git(wt, "branch", "--show-current").strip() == p.branch
    (wt / "a.txt").write_text("a\nb\n")
    git(wt, "commit", "-qam", "send_file")
    imp.built(p, "done", "ruff ok, mypy ok, 10 passed")
    assert imp.get(p.id).status == "ready" and [x.id for x in imp.to_notify()] == [p.id]
    imp.mark_notified(imp.get(p.id))
    assert imp.to_notify() == []
    imp.apply(imp.get(p.id))
    assert (paths.repo / "a.txt").read_text() == "a\nb\n" and not wt.exists()
    assert p.branch not in git(paths.repo, "branch") and imp.get(p.id).status == "applied"


def test_nothing_committed_is_a_failure_and_reject_cleans_up(paths: LawPaths) -> None:
    imp = Improvements(paths)
    p = imp.add("x", "y", "z", "owner")
    wt = imp.prepare(p)
    imp.built(p, "done", "not worth it")
    assert imp.get(p.id).status == "failed"
    with pytest.raises(ValueError, match="not ready"):
        imp.apply(imp.get(p.id))
    imp.reject(imp.get(p.id))
    assert not wt.exists() and p.branch not in git(paths.repo, "branch")


def test_payload_and_message(paths: LawPaths) -> None:
    imp = Improvements(paths)
    p = imp.add("Tony pads requests", "memo got a 5th section", "forbid invented sections", "tony")
    payload = build_payload(p, Path("/w"))
    assert payload["role"] == "coder" and "CLAUDE.md" in payload["prompt"] and p.branch in payload["prompt"]
    assert "{" not in BUILD_PROMPT.format(branch="b", problem="p", evidence="e", change="c")
    with pytest.raises(ValueError):
        imp.add(" ", "e", "c", "tony")
    with pytest.raises(KeyError):
        imp.get("../../x")
    p.status, p.result = "ready", "all checks pass"
    assert "Tony noticed" in message(p, paths) and "Apply merges" in message(p, paths)
