"""LAW Console endpoints: host stats, persona editing (comments preserved), Telegram settings."""

from __future__ import annotations

import shutil
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from law import console_edit, secrets
from law.api import create_app
from law.paths import LawPaths
from law.queue.store import JobStore

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def setup(sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "settings", repo / "settings")
    paths = LawPaths(repo=repo, home=sandbox / "LAW", user_home=sandbox)
    applied: list[str] = []
    monkeypatch.setattr(console_edit, "apply_async", lambda p, steps: applied.append(steps))
    monkeypatch.setattr(console_edit.telegram_setup, "get_me",
                        lambda tok: {"username": "TonyBot"} if tok.startswith("9876") else None)
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    app = create_app(paths, store=store, resolve=lambda j: j)
    with TestClient(app) as client:
        yield client, paths, applied


def test_system_reports_host_numbers(setup):
    client, _, _ = setup
    s = client.get("/system").json()
    assert s["memory"]["total_gb"] > 8 and 0 <= s["memory"]["percent"] <= 100
    assert s["disk"]["total_gb"] > 0 and s["disk"]["free_gb"] > 0
    assert "loaded_models" in s


def test_console_page_served(setup):
    client, _, _ = setup
    r = client.get("/console")
    assert r.status_code == 200 and "LAW Console" in r.text
    assert client.get("/").status_code == 200


def test_edit_persona_preserves_comments_and_reapplies(setup):
    client, paths, applied = setup
    roles = client.get("/roles").json()
    assert roles["reviewer"]["model"].startswith("gemma")
    body = {**{k: roles["analyst"][k] for k in ("summary", "temperature", "reasoning", "tools")},
            "prompt": "You are a quantitative analyst for a regional bank.\nAlways show units."}
    assert client.put("/roles/analyst", json=body).status_code == 200
    text = (paths.settings / "roles.yaml").read_text()
    assert "# Tool access per role is set in `tools:`" in text  # comments kept
    assert "for a regional bank." in text
    assert client.get("/roles").json()["analyst"]["prompt"].startswith("You are a quantitative analyst for a regional")
    assert applied == ["opencode-config,services"]


@pytest.mark.parametrize("change,status", [({"tools": ["shell", "teleport"]}, 422), ({"temperature": 3}, 422),
                                           ({"reasoning": "max"}, 422)])
def test_invalid_persona_edits_rejected(setup, change, status):
    client, paths, applied = setup
    r = client.get("/roles").json()["coder"]
    body = {**{k: r[k] for k in ("summary", "prompt", "temperature", "reasoning", "tools")}, **change}
    before = (paths.settings / "roles.yaml").read_text()
    assert client.put("/roles/coder", json=body).status_code == status
    assert (paths.settings / "roles.yaml").read_text() == before and applied == []


def test_unknown_role_is_404(setup):
    client, _, _ = setup
    body = {"summary": "abc", "prompt": "x" * 30, "temperature": 0.3, "reasoning": "low", "tools": []}
    assert client.put("/roles/nope", json=body).status_code == 404


def test_telegram_settings_verify_token_and_never_return_it(setup):
    client, paths, applied = setup
    good = "987654321:" + "A" * 35
    assert client.put("/settings/telegram", json={"token": "1:" + "B" * 35, "user_ids": "42"}).status_code == 422
    r = client.put("/settings/telegram", json={"token": good, "user_ids": "512345678, 42"})
    assert r.json() == {"bot": "TonyBot"} and applied == ["secrets,services"]
    assert secrets.read(paths.env_file)["LAW_TELEGRAM_USER_IDS"] == "512345678,42"
    s = client.get("/settings").json()
    assert s["telegram"] == {"configured": True, "bot": "TonyBot", "user_ids": ["42", "512345678"]}
    assert good not in client.get("/settings").text
    assert client.put("/settings/telegram", json={"token": None, "user_ids": "abc"}).status_code == 422


def test_client_config_uses_the_address_the_client_used(setup):
    client, _, _ = setup
    cfg = client.get("/client/opencode.json", headers={"host": "my-mac.local:8100"}).json()
    assert cfg["provider"]["law"]["options"]["baseURL"] == "http://my-mac.local:8000/v1"
    assert cfg["model"] == cfg["small_model"] == "law/qwen3.8-27b-8bit"
    assert "mcp" not in cfg  # Mac-only tools are not offered to other machines


def test_client_install_script_is_valid_shell(setup, tmp_path):
    import shutil as _sh
    import subprocess as _sp

    client, _, _ = setup
    script = client.get("/client/install.sh", headers={"host": "my-mac.local:8100"}).text
    assert 'SERVER="my-mac.local"' in script and "OPENCODE_DISABLE_MODELS_FETCH=1" in script
    path = tmp_path / "install.sh"
    path.write_text(script)
    if _sh.which("shellcheck"):
        res = _sp.run(["shellcheck", "-s", "sh", str(path)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, res.stdout


@pytest.mark.parametrize("login_shell,profile", [("/bin/zsh", ".zshrc"), ("/bin/bash", ".bashrc"), ("/usr/bin/fish", ".profile")])
def test_client_install_writes_the_login_shells_profile_once(setup, tmp_path, login_shell, profile):
    import subprocess as _sp

    client, _, _ = setup
    script = client.get("/client/install.sh", headers={"host": "127.0.0.1:8100"}).text
    # Run only the profile part: skip the reachability check and download (no server in unit tests).
    body = script.split('curl -fsS "http://$SERVER:8100/client/opencode.json"', 1)[1]
    home = tmp_path / "clienthome"
    home.mkdir()
    for _ in range(2):
        _sp.run(["/bin/sh", "-c", "set -eu\nSERVER=127.0.0.1\n" + body.split("\n", 1)[1]], check=True,
                env={"HOME": str(home), "SHELL": login_shell, "PATH": "/usr/bin:/bin"}, capture_output=True)
    text = (home / profile).read_text()
    assert text.count("# >>> law-client >>>") == 1
    assert 'export OPENCODE_CONFIG="$HOME/.config/law-client/opencode.json"' in text
