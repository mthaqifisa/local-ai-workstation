"""The privacy rules in .gitleaks.toml: each catches a realistic positive and passes its allowed examples.

Positives are assembled at runtime so this file itself stays clean under the same rules.
"""

from __future__ import annotations

import getpass
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / ".gitleaks.toml"
GITLEAKS = shutil.which("gitleaks")

pytestmark = pytest.mark.skipif(GITLEAKS is None, reason="gitleaks not installed (brew install gitleaks)")


def rules_hit(text: str, tmp_path: Path) -> set[str]:
    """Rule IDs gitleaks reports for `text`, using the repo's config."""
    assert GITLEAKS is not None
    report = tmp_path / "report.json"
    proc = subprocess.run([GITLEAKS, "stdin", "--no-banner", "--log-level", "error", "--config", str(CONFIG),
                           "--report-format", "json", "--report-path", str(report)],
                          input=text, capture_output=True, text=True, check=False)
    assert proc.returncode in (0, 1), proc.stderr
    return {f["RuleID"] for f in json.loads(report.read_text() or "[]")}


AT = "@"
POSITIVES = {
    "personal-email": ["mail me at jane.doe" + AT + "proton.me today", "MASTER_EMAIL=ann" + AT + "pm.me"],
    "home-path": ["open /Users/" + "janedoe/Projects/app", "cd /home/" + "jdoe/src",
                  "scratch -Users-" + "janedoe-SourceCode-app"],
    "mac-hostname": ["author jdoe" + AT + "Janes-Mac" + "Book-Pro-M5" + ".lo" + "cal", "ssh studio-2" + ".lo" + "cal"],
    "lan-address": ["LAW_TRUSTED_NETS=192.168." + "100.0/24", "router at 10." + "20.30.40", "172." + "20.1.9"],
}
ALLOWED = {
    "personal-email": ["you" + AT + "example.com", "34667065+mthaqifisa" + AT + "users.noreply.github.com",
                       "GitHub <noreply" + AT + "github.com>", "git" + AT + "github.com:org/repo.git",
                       "@app.get('/health')", "import x from '@types/node'", "react" + AT + "18.3.1"],
    "home-path": ["/Users/you/LAW", "/Users/runner/work", "/home/x/a", "-Users-you-SourceCode", "$HOME/LAW",
                  "~/LAW/state"],
    "mac-hostname": ["http://my-mac" + ".lo" + "cal:8080", "My-Mac" + ".lo" + "cal", "threading" + ".lo" + "cal()",
                     "-Dmaven.repo" + ".lo" + "cal=~/LAW/cache/m2", "~/.local/share"],
    "lan-address": ["192.168.1.20", "192.168.1.0/24", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                    "127.0.0.1", "0.0.0.0"],
}


@pytest.mark.parametrize(("rule", "text"), [(r, t) for r, ts in POSITIVES.items() for t in ts])
def test_rule_catches_realistic_positive(rule: str, text: str, tmp_path: Path) -> None:
    assert rule in rules_hit(text, tmp_path)


@pytest.mark.parametrize(("rule", "text"), [(r, t) for r, ts in ALLOWED.items() for t in ts])
def test_rule_passes_allowed_example(rule: str, text: str, tmp_path: Path) -> None:
    assert rule not in rules_hit(text, tmp_path)


def test_config_never_names_the_owner() -> None:
    """The rules describe shapes; the only literal addresses are placeholders and noreply ones."""
    text = CONFIG.read_text()
    assert getpass.getuser() not in text
    addresses = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", text)
    assert addresses == [], addresses


def test_default_secret_rules_still_apply(tmp_path: Path) -> None:
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    assert rules_hit(f"GITHUB_TOKEN={token}", tmp_path)
