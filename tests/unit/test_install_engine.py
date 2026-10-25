"""Installer engine and file-only steps, against a sandboxed HOME."""

from __future__ import annotations

from pathlib import Path

import pytest

from law import registry, roles, secrets
from law.install import engine
from law.install.steps import Ctx, EnvProfile, OpenCodeConfig, Secrets, Skeleton
from law.paths import LawPaths
from law.sysapi import Result, SysAPI

REPO = Path(__file__).resolve().parents[2]


class FakeSys(SysAPI):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, cmd, *, env=None, timeout=1800, cwd=None):  # type: ignore[no-untyped-def,override]
        self.calls.append(cmd)
        return Result(0, "")

    def launchd_loaded(self, label: str) -> bool:
        return False


@pytest.fixture()
def ctx(sandbox: Path) -> Ctx:
    paths = LawPaths(repo=REPO, home=sandbox / "LAW", user_home=sandbox)
    return Ctx(paths, registry.load(paths.registry_file), FakeSys())


def file_steps(c: Ctx) -> list[engine.Step]:
    return [Skeleton(c), EnvProfile(c), Secrets(c), OpenCodeConfig(c)]


def test_fresh_run_then_rerun_is_noop(ctx: Ctx):
    first = engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    assert first.ok and first.changes > 0
    second = engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    assert second.ok and second.changes == 0


def test_dry_run_writes_nothing(ctx: Ctx):
    report = engine.run(ctx.paths, file_steps(ctx), dry_run=True)
    assert report.changes > 0
    assert not (ctx.paths.home / "config").exists()


def test_zprofile_block_added_once_and_existing_lines_kept(ctx: Ctx):
    zp = ctx.paths.user_home / ".zprofile"
    zp.write_text('eval "$(/opt/homebrew/bin/brew shellenv zsh)"\n')
    for _ in range(3):
        engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    text = zp.read_text()
    assert text.count("# >>> LAW v6 >>>") == 1
    assert text.startswith('eval "$(/opt/homebrew/bin/brew shellenv zsh)"\n')


def test_stale_zprofile_block_is_replaced(ctx: Ctx):
    zp = ctx.paths.user_home / ".zprofile"
    zp.write_text("# >>> LAW v6 >>>\nold stuff\n# <<< LAW v6 <<<\nalias x=y\n")
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    text = zp.read_text()
    assert "old stuff" not in text and "alias x=y" in text and text.count("# >>> LAW v6 >>>") == 1


def test_secrets_generated_once_and_private(ctx: Ctx):
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    first = secrets.read(ctx.paths.env_file)
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    assert secrets.read(ctx.paths.env_file) == first
    assert oct(ctx.paths.env_file.stat().st_mode)[-3:] == "600"
    assert all(first[k] for k in secrets.GENERATED)


def test_imported_secrets_applied(ctx: Ctx):
    ctx.imported_secrets = {"LAW_TELEGRAM_TOKEN": "123:abc", "LAW_TELEGRAM_USER_IDS": "42, 7"}
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    values = secrets.read(ctx.paths.env_file)
    assert values["LAW_TELEGRAM_TOKEN"] == "123:abc"
    assert secrets.allowed_telegram_ids(values) == {42, 7}


def test_permission_drift_is_repaired(ctx: Ctx):
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    ctx.paths.env_file.chmod(0o644)
    report = engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    assert report.changes >= 1
    assert oct(ctx.paths.env_file.stat().st_mode)[-3:] == "600"


def test_deleted_law_home_is_rebuilt(ctx: Ctx):
    import shutil

    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    shutil.rmtree(ctx.paths.home)
    report = engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    assert report.ok and ctx.paths.env_sh.exists() and ctx.paths.env_file.exists()


def test_lock_prevents_concurrent_runs(ctx: Ctx):
    with engine.install_lock(ctx.paths), pytest.raises(engine.LockedError):
        engine.run(ctx.paths, file_steps(ctx), dry_run=False)


def test_failed_step_stops_run_and_next_run_resumes(ctx: Ctx):
    class Boom:
        name = "boom"
        fail = True

        def plan(self):
            return ["do it"] if self.fail else []

        def apply(self):
            if self.fail:
                raise RuntimeError("network down")

        def verify(self):
            return []

    boom = Boom()
    steps = [Skeleton(ctx), boom, EnvProfile(ctx)]
    r1 = engine.run(ctx.paths, steps, dry_run=False)
    assert not r1.ok and r1.steps[-1].name == "boom"
    assert not ctx.paths.env_sh.exists()
    boom.fail = False
    r2 = engine.run(ctx.paths, steps, dry_run=False)
    assert r2.ok and ctx.paths.env_sh.exists()


def test_opencode_config_uses_registry_models(ctx: Ctx):
    import json

    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    cfg = json.loads((ctx.paths.config / "opencode.json").read_text())
    assert cfg["model"] == "law/qwen3.8-27b-8bit"
    assert set(cfg["provider"]["law"]["models"]) >= {"qwen3.8-27b-8bit", "gemma-4-31b-8bit"}


def test_role_configs_carry_the_roles_reasoning_effort(ctx: Ctx):
    """Unset, the agent's model thinks unbounded (20-45 min between tool calls in a real run)."""
    import json

    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    coder = json.loads((ctx.paths.config / "opencode-coder.json").read_text())
    model = coder["model"].removeprefix("law/")
    assert coder["provider"]["law"]["models"][model]["options"] == {"reasoningEffort": "low"}
    architect = json.loads((ctx.paths.config / "opencode-architect.json").read_text())
    effort = roles.load(REPO / "settings" / "roles.yaml").get("architect").reasoning
    assert architect["provider"]["law"]["models"][architect["model"].removeprefix("law/")]["options"] == {
        "reasoningEffort": effort}


def test_registry_rejects_over_budget_model():
    import yaml

    data = yaml.safe_load((REPO / "settings" / "registry.yaml").read_text())
    data["llm_models"][0]["mem_gb"] = 99
    with pytest.raises(ValueError, match="budget"):
        registry.Registry.model_validate(data)


def test_registry_rejects_duplicate_ports():
    import yaml

    data = yaml.safe_load((REPO / "settings" / "registry.yaml").read_text())
    data["ports"]["homepage"] = data["ports"]["open_webui"]
    with pytest.raises(ValueError, match="unique"):
        registry.Registry.model_validate(data)


def test_interactive_without_terminal_rerun_is_noop(ctx: Ctx, monkeypatch: pytest.MonkeyPatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ctx.interactive = True
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    assert engine.run(ctx.paths, file_steps(ctx), dry_run=False).changes == 0


def test_telegram_setup_runs_while_unset_and_never_once_set(ctx: Ctx, monkeypatch: pytest.MonkeyPatch):
    import sys

    from law import telegram_setup

    runs: list[int] = []
    answers = iter([{}, {"LAW_TELEGRAM_TOKEN": "1:" + "A" * 35, "LAW_TELEGRAM_USER_IDS": "42"}])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(telegram_setup, "configure", lambda: runs.append(1) or next(answers))
    emails: list[int] = []
    monkeypatch.setattr(telegram_setup, "ask_email",
                        lambda: emails.append(1) or {"LAW_REPORT_EMAIL": "me@example.com"})
    ctx.interactive = True
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)  # user skips
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)  # asked again, user configures
    assert len(runs) == 2
    assert secrets.read(ctx.paths.env_file)["LAW_TELEGRAM_USER_IDS"] == "42"
    engine.run(ctx.paths, file_steps(ctx), dry_run=False)  # configured: never asked again
    assert len(runs) == 2
    assert emails == [1] and secrets.read(ctx.paths.env_file)["LAW_REPORT_EMAIL"] == "me@example.com"
    assert engine.run(ctx.paths, file_steps(ctx), dry_run=False).changes == 0

def test_launchd_reload_waits_for_async_bootout(ctx: Ctx):
    """launchctl bootout returns before the job is gone; apply() must wait, then bootstrap."""
    from law.install import launchd

    class AsyncLaunchd(FakeSys):
        def __init__(self) -> None:
            super().__init__()
            self.loaded = True
            self.polls_until_gone = 0

        def launchd_loaded(self, label: str) -> bool:
            if self.polls_until_gone > 0:
                self.polls_until_gone -= 1
                return True
            return self.loaded

        def launchd_bootout(self, label: str) -> Result:
            self.loaded = False
            self.polls_until_gone = 3  # still reported loaded for a few polls
            return Result(0, "")

        def launchd_bootstrap(self, plist: Path) -> Result:
            self.loaded = True
            self.bootstrapped = True
            return Result(0, "")

    sysapi = AsyncLaunchd()
    agent = launchd.Agent("com.law6.test", ["/bin/true"])
    ctx.paths.launch_agents.mkdir(parents=True, exist_ok=True)
    launchd.plist_path(ctx.paths, agent.label).write_text("stale")
    launchd.apply(ctx.paths, agent, "stamp", sysapi, timeout_s=5)
    assert getattr(sysapi, "bootstrapped", False)
    assert sysapi.loaded


def _seed_tool_bins(ctx: Ctx) -> None:
    """Pretend the tools' executables exist (the fake SysAPI installs nothing)."""
    for t in ctx.reg.tools:
        bin_dir = ctx.paths.runtime / (t.name if t.own_venv else ".") / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / t.check).write_text("#!/bin/sh\n")


def test_media_tools_install_once_then_in_sync(ctx: Ctx):
    from law.install.steps import MediaTools

    step = MediaTools(ctx)
    _seed_tool_bins(ctx)
    assert len(step.plan()) == len(ctx.reg.tools)
    step.apply()
    calls = ctx.sys.calls  # type: ignore[attr-defined]
    cmds = [c for c in calls if c[:3] == ["uv", "tool", "install"]]
    venv_installs = [c for c in calls if c[:3] == ["uv", "pip", "install"]]
    assert len(cmds) + len(venv_installs) == len(ctx.reg.tools)
    link = ctx.paths.runtime / "bin" / "mlxgen"
    assert link.is_symlink() and link.resolve() == (ctx.paths.runtime / "mlxgen" / "bin" / "mlxgen").resolve()
    assert not (ctx.paths.runtime / "bin" / "python").exists()  # acestep's venv python is never put on PATH
    mlx_audio = next(c for c in cmds if any("mlx-audio" in x for x in c))
    assert "misaki[en]" in mlx_audio and any("en_core_web_sm" in x for x in mlx_audio)
    assert step.plan() == []


def test_media_tools_reinstall_when_pin_changes(ctx: Ctx):
    from law.install.steps import MediaTools

    step = MediaTools(ctx)
    _seed_tool_bins(ctx)
    step.apply()
    stamp = ctx.paths.state / "tools" / "mflux.stamp"
    stamp.write_text(stamp.read_text().replace("0.23.0", "0.22.0"))
    assert step.plan() == ["install mflux==0.23.0"]


def test_homepage_links_use_lan_hostname(ctx: Ctx):
    from law.install.steps import Compose

    class HostSys(FakeSys):
        def run(self, cmd, *, env=None, timeout=1800, cwd=None):  # type: ignore[no-untyped-def,override]
            if cmd[:2] == ["scutil", "--get"]:
                return Result(0, "My-Mac\n")
            return super().run(cmd, env=env, timeout=timeout, cwd=cwd)

    ctx.sys = HostSys()
    services = Compose(ctx).homepage_files()["services.yaml"]
    assert f"http://My-Mac.local:{ctx.reg.ports.open_webui}" in services
    assert "localhost" not in services.split("siteMonitor")[0]


def test_compose_env_never_contains_repo_secrets(ctx: Ctx):
    from law.install.steps import Compose

    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    env = Compose(ctx).env()
    assert env["WEBUI_SECRET_KEY"] == secrets.read(ctx.paths.env_file)["WEBUI_SECRET_KEY"]
    assert "LAW_TELEGRAM_TOKEN" not in env


def test_registry_change_restarts_only_registry_readers(ctx: Ctx):
    from law.install import launchd

    reader = launchd.Agent("com.law6.worker", ["/bin/law", "worker"], reads_registry=True)
    plain = launchd.Agent("com.law6.colima", ["/opt/homebrew/bin/colima", "start"])
    assert reader.render(ctx.paths, "aaa") != reader.render(ctx.paths, "bbb")
    assert plain.render(ctx.paths, "aaa") == plain.render(ctx.paths, "bbb")


def test_media_models_prefetch_pinned_once(ctx: Ctx):
    from law.install.steps import MediaModels

    step = MediaModels(ctx)
    planned = step.plan()
    assert any("Qwen/Qwen-Image-2.1@d26bb612" in x for x in planned)
    assert not any("SeedVR2" in x for x in planned)  # prefetch disabled
    assert len([x for x in planned if "Qwen-Image-2.1" in x]) == 1  # image + edit share one repo
    assert any("ACE-Step/acestep-5Hz-lm-4B@0a3ec94b" in x for x in planned)  # pinned companion repo
    assert not any("prince-canuma" in x for x in planned)  # unpinned companions are left to the tool
    lightning = [x for x in planned if "Wan2.2-Lightning" in x]
    assert len(lightning) == 4 and all("high_noise" in x or "low_noise" in x for x in lightning)  # files, not repo
    step.apply()
    downloads = [c for c in ctx.sys.calls if len(c) > 1 and c[1] == "download"]  # type: ignore[attr-defined]
    assert all("--revision" in c for c in downloads)
    assert step.plan() == []


def test_small_profile_skips_media_prefetch(ctx: Ctx):
    from law.install.steps import MediaModels

    ctx.profile = "small"
    assert MediaModels(ctx).plan() == []


def test_env_sh_never_hijacks_other_uv_projects(ctx: Ctx):
    """UV_PROJECT_ENVIRONMENT in the shell would make `uv sync` in ANY project overwrite LAW's own venv."""
    from law.install.steps import EnvProfile

    text = EnvProfile(ctx).render()
    assert "UV_PROJECT_ENVIRONMENT" not in text
    assert "VIRTUAL_ENV" not in text


def test_opencode_agents_follow_role_tools(ctx: Ctx):
    import json

    from law.install.steps import OpenCodeConfig

    cfg = json.loads(OpenCodeConfig(ctx).render())
    agents = cfg["agent"]
    assert agents["operator"]["tools"]["mac_*"] is True
    assert agents["coder"]["tools"]["mac_*"] is False and agents["coder"]["tools"]["bash"] is True
    assert agents["reviewer"]["tools"]["write"] is False and agents["reviewer"]["tools"]["edit"] is False
    assert agents["reviewer"]["tools"]["read"] is True and agents["reviewer"]["tools"]["bash"] is True
    assert agents["reviewer"]["model"] == "law/gemma-4-31b-8bit"
    assert "senior software engineer" in agents["coder"]["prompt"]  # role prompt + house standard
    assert "Production quality only" in agents["coder"]["prompt"]
    assert cfg["mcp"]["mac"]["command"][-1] == "mcp-mac"


def test_each_role_config_uses_one_model_only(ctx: Ctx):
    """OpenCode's housekeeping model must equal the role's model: two large models do not fit at once."""
    import json

    from law.install.steps import OpenCodeConfig

    files = OpenCodeConfig(ctx).files()
    coder = json.loads(files[ctx.paths.config / "opencode-coder.json"])
    assert coder["model"] == coder["small_model"] == "law/qwen3.8-27b-8bit"
    operator = json.loads(files[ctx.paths.config / "opencode-operator.json"])
    assert operator["model"] == operator["small_model"]
    roles = len(json.loads(files[ctx.paths.config / "opencode.json"])["agent"])
    assert len(files) == 1 + roles * 5  # the role's default plus one file per thinking level (off..xhigh)
    high = json.loads(files[ctx.paths.config / "opencode-coder-xhigh.json"])
    assert high["provider"]["law"]["models"]["qwen3.8-27b-8bit"]["options"] == {"reasoningEffort": "xhigh"}
    assert high["model"] == high["small_model"] == coder["model"]


def test_code_change_changes_service_stamp(ctx: Ctx, tmp_path: Path):
    """After `git pull` with new code, api/worker/telegram must restart (re-run guarantee 5)."""
    import shutil

    repo = tmp_path / "repo"
    shutil.copytree(REPO / "settings", repo / "settings")
    shutil.copytree(REPO / "src", repo / "src")
    ctx.paths = LawPaths(repo=repo, home=ctx.paths.home, user_home=ctx.paths.user_home)
    before = ctx.stamp
    (repo / "src" / "law" / "worker_main.py").write_text("# changed\n")
    assert ctx.stamp != before
    after_code = ctx.stamp
    (repo / "settings" / "roles.yaml").write_text((repo / "settings" / "roles.yaml").read_text() + "\n")
    assert ctx.stamp != after_code


def test_omlx_disk_cache_cap_and_idle_unload_are_set_idempotently(ctx: Ctx):
    import json

    from law.install.steps import OmlxSettings

    class Kick(FakeSys):
        def __init__(self) -> None:
            super().__init__()
            self.kicked: list[str] = []

        def launchd_kickstart(self, label: str) -> Result:
            self.kicked.append(label)
            return Result(0, "")

    ctx.sys = Kick()
    step = OmlxSettings(ctx)
    assert step.plan() == []  # oMLX hasn't created its settings yet
    step.file.parent.mkdir(parents=True, exist_ok=True)
    step.file.write_text(json.dumps({"cache": {"enabled": True, "ssd_cache_max_size": "auto"}, "memory": {"x": 1}}))
    assert step.plan() == ["set oMLX cache.ssd_cache_max_size = 20GB", "set oMLX idle_timeout.idle_timeout_seconds = 300"]
    step.apply()
    doc = json.loads(step.file.read_text())
    assert doc["cache"]["ssd_cache_max_size"] == "20GB" and doc["memory"] == {"x": 1}
    assert doc["idle_timeout"] == {"idle_timeout_seconds": 300}
    assert ctx.sys.kicked == ["com.law6.omlx"] and step.plan() == []


def test_openwebui_state_restores_only_when_saved_files_change(ctx: Ctx):
    from law.install.steps import OpenWebUIState

    step = OpenWebUIState(ctx)
    step.folder = ctx.paths.user_home / "repo-settings" / "openwebui"  # never write into the real repo
    assert step.plan() == []  # nothing saved yet
    step.folder.mkdir(parents=True)
    (step.folder / "prompts.json").write_text('[{"command": "memo", "name": "Memo", "content": "Write"}]\n')
    assert step.plan() == ["restore saved Open WebUI presets, prompts and knowledge"]
    step.stamp.parent.mkdir(parents=True, exist_ok=True)
    step.stamp.write_text(step._digest() + "\n")  # what apply() records after a restore
    assert step.plan() == []
    (step.folder / "prompts.json").write_text("[]\n")
    assert step.plan() != []


def test_worker_restart_waits_while_jobs_run(ctx: Ctx, monkeypatch: pytest.MonkeyPatch):
    from law.install import launchd, steps

    services = steps.Services(ctx)
    worker = next(a for a in services.agents() if a.label.endswith("worker"))
    plist = launchd.plist_path(ctx.paths, worker.label)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(worker.render(ctx.paths, "old-stamp"))  # installed before a code change
    monkeypatch.setattr(steps, "jobs_running", lambda reg: 2)
    assert services._defer_worker(worker) is True  # code-only change: written, restart deferred
    assert launchd.stamp_of(plist.read_bytes()) == ctx.stamp
    assert (ctx.paths.state / steps.WORKER_RESTART_FLAG).exists()
    monkeypatch.setattr(steps, "jobs_running", lambda reg: 0)
    plist.write_bytes(worker.render(ctx.paths, "old-stamp"))
    assert services._defer_worker(worker) is False  # idle: restart normally
    monkeypatch.setenv("LAW_FORCE_RESTART", "1")
    monkeypatch.setattr(steps, "jobs_running", lambda reg: 2)
    assert services._defer_worker(worker) is False


def test_formula_installed_without_its_brewfile_options_is_reinstalled(ctx: Ctx, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch):
    import json

    from law.install import steps

    cellar = tmp_path / "Cellar" / "omlx"
    (cellar / "0.7.1").mkdir(parents=True)
    receipt = cellar / "0.7.1" / "INSTALL_RECEIPT.json"
    receipt.write_text(json.dumps({"used_options": []}))  # installed before the option was in the Brewfile

    class BrewSys(FakeSys):
        def run(self, cmd, *, env=None, timeout=1800, cwd=None):  # type: ignore[no-untyped-def,override]
            self.calls.append(cmd)
            if cmd[:2] == ["brew", "--cellar"]:
                return Result(0, str(cellar) if cmd[2] == "jundot/omlx/omlx" else str(tmp_path / "none"))
            return Result(0, "")

    ctx.sys = BrewSys()
    monkeypatch.setattr(steps, "jobs_running", lambda reg: 0)
    brew = steps.Brew(ctx)
    assert brew.wanted_options()["jundot/omlx/omlx"] == ["--with-custom-kernel"]
    assert "reinstall jundot/omlx/omlx with --with-custom-kernel" in brew.plan()
    brew.apply()
    assert ["brew", "reinstall", "jundot/omlx/omlx", "--with-custom-kernel"] in ctx.sys.calls
    receipt.write_text(json.dumps({"used_options": ["--with-custom-kernel"]}))
    assert not any(p.startswith("reinstall") for p in brew.plan())


def test_thinking_off_turns_the_chat_template_switch(ctx: Ctx):
    import json

    from law.install.steps import OpenCodeConfig, model_options

    assert model_options("off") == {"chat_template_kwargs": {"enable_thinking": False}}
    assert model_options("low") == {"reasoningEffort": "low"}
    off = json.loads(OpenCodeConfig(ctx).files()[ctx.paths.config / "opencode-writer-off.json"])
    assert off["provider"]["law"]["models"]["qwen3.8-27b-8bit"]["options"] == {
        "chat_template_kwargs": {"enable_thinking": False}}


def test_api_tools_installs_the_pinned_typescript_once(ctx: Ctx):
    import json

    from law.install.steps import ApiTools

    class Npm(FakeSys):
        def run(self, cmd, *, env=None, timeout=1800, cwd=None):  # type: ignore[no-untyped-def,override]
            self.calls.append(cmd)
            prefix = Path(cmd[cmd.index("--prefix") + 1])
            pkg = json.loads((prefix / "package.json").read_text())
            ts = prefix / "node_modules" / "typescript"
            ts.mkdir(parents=True, exist_ok=True)
            (ts / "package.json").write_text(json.dumps({"version": pkg["dependencies"]["typescript"]}))
            return Result(0, "")

    ctx.sys = Npm()
    step = ApiTools(ctx)
    assert step.plan() == [f"install typescript {ApiTools.TYPESCRIPT} for law api"]
    step.apply()
    assert ctx.sys.calls[0][:3] == ["npm", "install", "--prefix"] and step.plan() == []


def test_agent_reply_limit_is_half_the_context_capped_at_32k():
    from law.install.steps import output_limit

    assert output_limit(98304) == 32768 and output_limit(32768) == 16384 and output_limit(None) == 16384
    assert output_limit(32768, writes=False) == 6144  # a judge's context goes to reading


def test_portainer_gets_its_port_and_generated_password_and_a_tile(ctx: Ctx):
    from law.install.steps import Compose

    engine.run(ctx.paths, file_steps(ctx), dry_run=False)
    env = Compose(ctx).env()
    assert env["PORTAINER_PORT"] == str(ctx.reg.ports.portainer) == "9000"
    assert len(env["PORTAINER_ADMIN_PASSWORD"]) >= 32
    assert env["PORTAINER_ADMIN_PASSWORD"] == secrets.read(ctx.paths.env_file)["PORTAINER_ADMIN_PASSWORD"]
    assert f":{ctx.reg.ports.portainer}" in Compose(ctx).homepage_files()["services.yaml"]
    compose = (REPO / "compose.yaml").read_text()
    assert "environment: PORTAINER_ADMIN_PASSWORD" in compose and "2375" not in compose  # secret file, no TCP socket
