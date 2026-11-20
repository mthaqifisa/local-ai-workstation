"""The committed hooks (.githooks/) block personal data and foreign identities on commit and push."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NOREPLY = "34667065+mthaqifisa" + "@users.noreply.github.com"
OTHER = "jane.doe" + "@" + "proton.me"

pytestmark = pytest.mark.skipif(shutil.which("gitleaks") is None or shutil.which("git-lfs") is None,
                                reason="needs gitleaks and git-lfs (Brewfile)")


def git(repo: Path, *args: str, email: str = NOREPLY, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_AUTHOR_NAME": "thaqif", "GIT_COMMITTER_NAME": "thaqif", "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_EMAIL": email, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=check)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch repo using copies of this repo's hooks and gitleaks config, with a bare 'origin'."""
    work, origin = tmp_path / "work", tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    shutil.copytree(REPO / ".githooks", work / ".githooks")
    shutil.copy(REPO / ".gitleaks.toml", work / ".gitleaks.toml")
    git(work, "config", "core.hooksPath", ".githooks")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "add", ".")
    git(work, "commit", "-qm", "hooks")
    git(work, "push", "-q", "origin", "main")
    return work


def commit_file(repo: Path, text: str, msg: str = "change", email: str = NOREPLY,
                verify: bool = True) -> subprocess.CompletedProcess[str]:
    (repo / "notes.md").write_text(text)
    git(repo, "add", "notes.md")
    return git(repo, "commit", "-qm", msg, *([] if verify else ["--no-verify"]), email=email, check=False)


def test_clean_commit_and_push_pass(repo: Path) -> None:
    assert commit_file(repo, "Paths: ~/LAW and $HOME. Mail you@example.com\n").returncode == 0
    assert git(repo, "push", "-q", "origin", "main", check=False).returncode == 0


def test_commit_with_home_path_is_blocked(repo: Path) -> None:
    res = commit_file(repo, "log: /Users/" + "janedoe/LAW/out.txt\n")
    assert res.returncode != 0 and "personal data" in res.stderr


def test_commit_with_personal_identity_is_blocked(repo: Path) -> None:
    res = commit_file(repo, "fine\n", email=OTHER)
    assert res.returncode != 0 and "noreply" in res.stderr


def test_commit_message_with_email_is_blocked(repo: Path) -> None:
    res = commit_file(repo, "fine\n", msg="reported by " + OTHER)
    assert res.returncode != 0 and "commit message" in res.stderr


def test_push_rejects_foreign_author_even_when_commit_hook_was_skipped(repo: Path) -> None:
    assert commit_file(repo, "fine\n", email=OTHER, verify=False).returncode == 0
    res = git(repo, "push", "-q", "origin", "main", check=False)
    assert res.returncode != 0 and "author/committer" in res.stderr


def test_push_rejects_personal_data_in_content(repo: Path) -> None:
    assert commit_file(repo, "LAN 192.168." + "100.7\n", verify=False).returncode == 0
    res = git(repo, "push", "-q", "origin", "main", check=False)
    assert res.returncode != 0 and "personal data" in res.stderr


def test_push_of_new_branch_checks_only_new_commits(repo: Path) -> None:
    git(repo, "checkout", "-qb", "feature")
    assert commit_file(repo, "ok\n").returncode == 0
    assert git(repo, "push", "-q", "origin", "feature", check=False).returncode == 0
