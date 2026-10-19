"""Phase 0 tests: fresh Mac, v1 Mac, reruns, drift repair, pruning, failures.
    cd tests && python -m pytest -q
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest
import yaml

from law import cli
from law.engine import Ctx, StepFailed, run_steps
from law.manifest import load_manifest
from law.paths import get_paths
from law.project import ensure_project, scaffold
from law.registry import RegistryError, load
from law.steps import build_steps
from law.util import atomic_write, file_lock, read_env

from fakes import FakeSystem

AUTO = [m.repo for m in load().models.values() if m.download == "auto"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("LAW_HOME", "HF_HOME", "HF_HUB_CACHE"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture
def fs(home):
    return FakeSystem(get_paths().hf_cache)


def install(fs, **kw):
    return run_steps(Ctx(paths=get_paths(), sys=fs, **kw), build_steps(), on_macos=True)


def changes(rep):
    return rep.added + rep.updated + rep.removed


def cache(home, repo, incomplete=False):
    d = get_paths().hf_cache / ("models--" + repo.replace("/", "--"))
    (d / "snapshots" / "x").mkdir(parents=True, exist_ok=True)
    (d / "blobs").mkdir(exist_ok=True)
    (d / "blobs" / "w.safetensors").write_bytes(b"0" * 10)
    if incomplete:
        (d / "blobs" / "w2.incomplete").write_bytes(b"0")
    return d


def make_v1(home: Path, fs: FakeSystem):
    w = home / ".mlx-ai-workstation"
    (w / ".venv" / "bin").mkdir(parents=True)
    (w / "searxng").mkdir()
    (w / ".env").write_text("HF_TOKEN=hf_abc\nTELEGRAM_BOT_TOKEN='123:xyz'\n"
                            "TELEGRAM_USER_ID=42\nDASHBOARD_PASSWORD=secret\n")
    (w / "models.custom.tsv").write_text(
        "mycoder\tsomeorg/Cool-Coder-4bit\tlm\tqwen3\t\n"
        "vision\tmlx-community/Qwen3-VL-8B-Instruct-4bit\tmultimodal\t\t\n")
    (w / "personas.json").write_text('{"x": {}}')
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    for s in ["inference", "gateway", "dashboard", "vision", "telegram", "colima", "docker-bridge"]:
        (la / f"com.mlxstack.{s}.plist").write_text("<plist/>")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "mlx-agent").write_text("#!/bin/sh\n")
    fs.brew |= {"uv", "git", "colima", "docker", "node"}
    fs.colima = {"status": "Running", "cpus": 6, "memory_gb": 8, "disk_gb": 80}
    for n in ("open-webui", "searxng", "portainer"):
        fs.containers[n] = {"state": "running", "image": "x", "labels": {}}
    fs.containers["langfuse-web-1"] = {"state": "running", "image": "lf",
                                       "labels": {"com.docker.compose.project": "langfuse"}}
    cache(home, "mlx-community/Qwen3.8-27B-4bit")              # still wanted
    cache(home, "richardyoung/Kimi-K2-Instruct-0905-MLX-4bit")  # v1-only → prune
    cache(home, "mlx-community/gemma-4-31B-4bit")               # v1-only → prune
    cache(home, "someone/my-own-model")                         # user's own → never touched
    return w


# ── fresh Mac ───────────────────────────────────────────────────────────
def test_fresh_mac_installs_everything_then_rerun_is_noop(home, fs):
    rep = install(fs)
    assert "failed" not in rep.results.values()
    assert set(load().brew_formulae) <= fs.brew
    assert fs.colima == {"status": "Running", "cpus": 4, "memory_gb": 4, "disk_gb": 60}
    assert set(fs.containers) == {"open-webui", "searxng", "portainer"}
    for repo in AUTO:
        assert f"dl {repo}" in fs.calls
    assert (get_paths().searxng / "settings.yml").exists()
    assert (home / "Library/LaunchAgents/com.law.colima.plist").exists()
    fs.calls.clear()
    assert changes(install(fs)) == 0 and fs.calls == [], fs.calls


def test_dry_run_changes_nothing(home, fs):
    rep = install(fs, dry_run=True)
    assert "would-change" in rep.results.values()
    assert fs.calls == [] and not (home / ".law").exists()


# ── v1 Mac ──────────────────────────────────────────────────────────────
def test_v1_mac_is_migrated_retired_and_pruned(home, fs):
    w = make_v1(home, fs)
    install(fs)
    p = get_paths()
    env = read_env(p.env_file)
    assert env["HF_TOKEN"] == "hf_abc" and env["TELEGRAM_USER_ID"] == "42"
    assert "DASHBOARD_PASSWORD" not in env
    assert stat.S_IMODE(p.env_file.stat().st_mode) == 0o600
    um = yaml.safe_load(p.user_models.read_text())["models"]
    assert [m["repo"] for m in um] == ["someorg/Cool-Coder-4bit"]
    assert p.user_personas.exists()
    assert not w.exists()
    assert not list((home / "Library/LaunchAgents").glob("com.mlxstack.*"))
    assert not (home / ".local/bin/mlx-agent").exists()
    bk = list(p.backups.glob("v1-*"))
    assert len(bk) == 1 and (bk[0] / "mlx-ai-workstation/.env").exists()
    assert not (bk[0] / "mlx-ai-workstation/.venv").exists()
    assert all(c["labels"].get("law.managed") == "1" for c in fs.containers.values())
    assert "langfuse-web-1" not in fs.containers
    assert fs.colima["memory_gb"] == 4 and fs.colima["disk_gb"] == 80      # disk never shrinks
    hub = p.hf_cache
    assert not (hub / "models--richardyoung--Kimi-K2-Instruct-0905-MLX-4bit").exists()
    assert not (hub / "models--mlx-community--gemma-4-31B-4bit").exists()
    assert (hub / "models--someone--my-own-model").exists()
    assert (hub / "models--mlx-community--Qwen3.8-27B-4bit").exists()
    assert len(list(p.trash.iterdir())) == 2
    fs.calls.clear()
    assert changes(install(fs)) == 0 and fs.calls == []


def test_keep_v1_leaves_v1_alone(home, fs):
    w = make_v1(home, fs)
    install(fs, keep_v1=True)
    assert w.exists() and (home / ".local/bin/mlx-agent").exists()
    assert read_env(get_paths().env_file)["HF_TOKEN"] == "hf_abc"
    assert (get_paths().hf_cache / "models--richardyoung--Kimi-K2-Instruct-0905-MLX-4bit").exists()


def test_existing_secret_never_overwritten(home, fs):
    make_v1(home, fs)
    p = get_paths()
    p.law.mkdir(parents=True)
    p.env_file.write_text("HF_TOKEN=mine\n")
    install(fs)
    assert read_env(p.env_file)["HF_TOKEN"] == "mine"


# ── drift repair ────────────────────────────────────────────────────────
def test_drift_is_repaired(home, fs):
    install(fs)
    p = get_paths()
    os.chmod(p.env_file, 0o644)
    fs.brew.discard("ffmpeg")
    fs.containers["searxng"]["state"] = "exited"
    fs.containers.pop("portainer")
    fs.colima["status"] = "Stopped"
    (p.generated / "registry.resolved.json").write_text("corrupt")
    (p.generated / "old-thing.yaml").write_text("x")
    fs.pkgs.clear()
    cache(home, AUTO[0], incomplete=True)
    fs.calls.clear()
    install(fs)
    assert stat.S_IMODE(p.env_file.stat().st_mode) == 0o600
    assert "brew+ffmpeg" in fs.calls and "start searxng" in fs.calls and "run portainer" in fs.calls
    assert "sync" in fs.calls and f"dl {AUTO[0]}" in fs.calls
    assert not (p.generated / "old-thing.yaml").exists()
    fs.calls.clear()
    assert changes(install(fs)) == 0


# ── removal when no longer needed ───────────────────────────────────────
def test_registry_removal_prunes_owned_only(home, fs):
    fs.brew.add("wget")                                        # was already there → not ours
    install(fs)
    p = get_paths()
    assert "ffmpeg" in load_manifest(p).data["brew"]
    assert "wget" not in load_manifest(p).data["brew"]
    import law.registry as R
    orig = R._builtin_text
    txt = yaml.safe_load(orig())
    txt["brew"]["formulae"] = [f for f in txt["brew"]["formulae"] if f not in ("ffmpeg", "wget", "jq")]
    txt["docker"]["containers"].pop("portainer")
    txt["models"] = [m for m in txt["models"] if m["id"] != "kokoro-82m"]
    R._builtin_text = lambda: yaml.safe_dump(txt)
    try:
        fs.brew_deps["jq"] = ["something"]
        fs.calls.clear()
        install(fs)
    finally:
        R._builtin_text = orig
    assert "brew-ffmpeg" in fs.calls
    assert "brew-wget" not in fs.calls
    assert "brew-jq" not in fs.calls
    assert "portainer" not in fs.containers
    assert not (p.hf_cache / "models--prince-canuma--Kokoro-82M").exists()


def test_user_model_download_manual_not_pulled(home, fs):
    p = get_paths()
    atomic_write(p.user_models, yaml.safe_dump({"models": [
        {"id": "mine", "repo": "me/x", "kind": "lm", "weights_gb": 3}]}))
    install(fs)
    assert "dl me/x" not in fs.calls


# ── failures ────────────────────────────────────────────────────────────
def test_not_enough_disk_fails_cleanly_then_resumes(home, fs):
    fs.free = 5
    with pytest.raises(StepFailed):
        install(fs)
    assert "needs" in (get_paths().logs / "install-error.log").read_text()
    fs.free = 500
    assert install(fs).results["models"] == "changed"
    assert changes(install(fs)) == 0


def test_failed_download_resumes_next_run(home, fs):
    fs.fail_download.add(AUTO[1])
    with pytest.raises(StepFailed):
        install(fs)
    assert load_manifest(get_paths()).model(AUTO[1]) == {"complete": False}
    fs.fail_download.clear(); fs.calls.clear()
    install(fs)
    assert f"dl {AUTO[1]}" in fs.calls and f"dl {AUTO[0]}" not in fs.calls


def test_upgrade_converges(home, fs):
    install(fs)
    fs.calls.clear()
    install(fs, upgrade=True)
    assert "compile" in fs.calls and "pull ghcr.io/open-webui/open-webui:main" in fs.calls
    fs.calls.clear()
    assert changes(install(fs)) == 0


# ── registry ────────────────────────────────────────────────────────────
def test_registry_valid(home):
    reg = load()
    assert reg.raw["defaults"]["persona"] == "general"
    assert reg.fits("qwen3-coder-next")[0]
    assert len(set(reg.ports.values())) == len(reg.ports)
    assert all("Devstral" not in m.repo for m in reg.models.values())
    runtime = " ".join(reg.python["runtime"])
    assert "mlx-vlm" not in runtime and "transformers" not in runtime   # pinned by mlx-openai-server


def test_registry_rejects_bad_input(home):
    bad = yaml.safe_load(resources.files("law").joinpath("registry.yaml").read_text())
    bad["ports"]["dashboard"] = bad["ports"]["gateway"]
    with pytest.raises(RegistryError, match="port"):
        load(builtin_text=yaml.safe_dump(bad))


def test_user_overlay_too_big_is_flagged(home):
    p = get_paths()
    atomic_write(p.user_models, yaml.safe_dump({"models": [
        {"id": "giant", "repo": "x/giant", "kind": "lm", "weights_gb": 300}]}))
    assert load(p).fits("giant")[0] is False


# ── .local-agent + misc ─────────────────────────────────────────────────
def test_init_project(home):
    proj = home / "MLX-AI" / "source" / "test"
    p = ensure_project(proj)
    assert p.is_dir() and not (p / ".local-agent").exists()          # folder made, nothing added inside
    la, created = scaffold(proj)                                     # law init-project writes the one opt-in file
    assert (la / "AGENT.md").exists() and created == ["AGENT.md"]
    (la / "AGENT.md").write_text("MY RULES")
    assert scaffold(proj)[1] == [] and (la / "AGENT.md").read_text() == "MY RULES"   # idempotent, never overwrites
    f = home / "afile"; f.write_text("x")
    with pytest.raises(NotADirectoryError):
        ensure_project(f)


def test_lock_blocks_second_holder(home):
    lock = home / "l.lock"
    with file_lock(lock):
        code = ("from law.util import file_lock,LockBusy\nimport sys\n"
                f"try:\n  with file_lock(__import__('pathlib').Path({str(lock)!r})): sys.exit(0)\n"
                "except LockBusy: sys.exit(7)")
        assert subprocess.run([sys.executable, "-c", code]).returncode == 7


def test_cli(home, capsys):
    assert cli.main(["--list-models"]) == 0
    assert "qwen38-27b" in capsys.readouterr().out
    assert cli.main(["--image"]) == 2                    # v1 flag now maps to `law image`; empty prompt
    assert cli.main(["init-project", str(home / "new")]) == 0
