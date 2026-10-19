"""`law` — the v2 control-plane CLI. Also accepts the v1 `--flag` style."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from . import agentcmds, backupcmds, mediacmds, runcmds, setupcmds
from .doctor import cached, report
from .engine import Ctx, StepFailed, run_steps
from .paths import get_paths
from .project import scaffold
from .registry import load
from .steps import build_steps
from .system import System
from .util import LockBusy, err, file_lock, head, is_macos, ok

# v1 flag → (v2 command, phase it arrives in; None = available now)
LEGACY = {
    "--bootstrap": ("install", None), "--wizard": ("install", None),
    "--status": ("status", None), "--list-models": ("models", None),
    "--init-project": ("init-project", None),
    "--start": ("start", None), "--stop": ("stop", None), "--restart": ("restart", None),
    "--configure": ("configure", None), "--pull-models": ("install", None), "--pull-heavy": ("install", None),
    "--pull-vision": ("install", None), "--download-model": ("pull", None), "--probe-model": ("probe", None),
    "--add-model": ("add-model", None), "--remove-model": ("remove-model", None),
    "--setup-opencode": ("setup-opencode", None), "--agent": ("agent", None),
    "--resume": ("agent", None), "--show-memory": ("agent", None),
    "--telegram": ("telegram", None), "--telegram-service": ("telegram", None),
    "--image": ("image", None), "--vision": ("vision", None),
}


def _translate_legacy(argv: list[str]) -> list[str]:
    if argv and argv[0] in LEGACY:
        cmd, phase = LEGACY[argv[0]]
        if phase is not None:
            err(f"'{argv[0]}' arrives in v2 Phase {phase} as `law {cmd}`.")
            sys.exit(2)
        if argv[0] in ("--resume", "--show-memory"):
            return [cmd, argv[0]] + argv[1:]
        if argv[0] == "--telegram-service":
            return [cmd, "status"] + argv[1:]
        return [cmd] + argv[1:]
    return argv


def cmd_install(a) -> int:
    p = get_paths()
    ctx = Ctx(paths=p, sys=System(), dry_run=a.dry_run, upgrade=a.upgrade, keep_v1=a.keep_v1, src=a.src)
    try:
        with file_lock(p.lock):
            rep = run_steps(ctx, build_steps(), on_macos=is_macos())
    except LockBusy as e:
        err(str(e) + " — wait for it to finish"); return 1
    except StepFailed:
        return 1
    head("Summary")
    if a.dry_run:
        would = [k for k, v in rep.results.items() if v == "would-change"]
        ok("dry-run: " + (("would change: " + ", ".join(would)) if would else "nothing to change"))
    elif rep.added + rep.updated + rep.removed == 0:
        ok("everything already up to date — nothing changed")
    else:
        ok(f"{rep.added} added · {rep.updated} updated · {rep.removed} removed")
    if not a.dry_run:
        from .telegram import configured
        print("  Next:  law status   ·   law agent \"<task>\" -C <folder>"
              + ("" if configured(p) else "   ·   law configure (Telegram)"))
    return 0


def cmd_status(a) -> int:
    return report(get_paths(), check_hf=a.models, check_updates=a.updates)


def cmd_models(a) -> int:
    p = get_paths()
    reg = load(p)
    order = {"core": 0, "heavy": 1, "optional": 2, "user": 3}
    rows = sorted(reg.models.values(), key=lambda m: (order.get(m.tier, 9), m.id))
    print(f"{'ID':<20} {'TIER':<8} {'GB':>5}  {'KIND':<7} {'GET':<6} {'CTX':>6}  {'STATE':<12} LABEL")
    for m in rows:
        state = "TOO BIG" if not reg.fits(m.id)[0] else ("downloaded" if cached(p, m.repo) else "-")
        mark = {"user": " (yours)", "override": " (your override)"}.get(m.source, "")
        print(f"{m.id:<20} {m.tier:<8} {m.weights_gb:>5g}  {m.kind:<7} {m.download:<6} {m.context or '-':>6}  "
              f"{state:<12} {m.label}{mark}")
        if a.verbose:
            print(f"{'':<20} {m.repo}  license={m.license}  verified={m.verified}  "
                  f"tool_parser={m.tool_parser}  reasoning_parser={m.reasoning_parser}")
    print(f"\nPersonas (default: {reg.raw.get('defaults', {}).get('persona')}):")
    for name, pd in reg.personas.items():
        print(f"  {name:<14} → {pd['model']:<18} {pd.get('desc', '')}")
    return 0


def cmd_init_project(a) -> int:
    try:
        la, created = scaffold(Path(a.dir))
    except (NotADirectoryError, PermissionError) as e:
        err(str(e)); return 1
    ok(f"{la}: " + (f"created {', '.join(created)}" if created
                    else "AGENT.md already present — nothing changed"))
    return 0


def cmd_purge(a) -> int:
    p = get_paths()
    if not a.yes:
        err("purge needs --yes"); return 2
    sysm = System()
    if is_macos() and sysm.docker_ok():
        for c in sysm.docker_containers():
            if c["labels"].get("law.managed") == "1":
                sysm.docker_rm(c["name"])
    for f in p.launch_agents.glob("com.law.*.plist") if p.launch_agents.is_dir() else []:
        if is_macos():
            sysm.launchd_unload(f.stem)
        f.unlink(missing_ok=True)
    from .runtime.modelproc import kill_from_pidfile
    kill_from_pidfile(p.run / "model.pid")
    for d in [p.generated, p.state, p.run, p.runtime, p.trash]:
        shutil.rmtree(d, ignore_errors=True)
    ok("removed: law services (incl. Telegram) + containers, runtime venv, generated files, state, trash")
    ok("kept: ~/.law/.env, ~/.law/config, ~/.law/backups, Docker volumes, models, brew packages, ~/MLX-AI, "
       "OpenCode config")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="law", description="Local AI Workstation v2")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("install", help="install / repair / upgrade / prune (safe to re-run)")
    s.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    s.add_argument("--upgrade", action="store_true", help="re-resolve packages, pull newer images")
    s.add_argument("--keep-v1", action="store_true", help="don't retire the v1 stack this run")
    s.add_argument("--src", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_install)

    for name in ("status", "doctor"):
        s = sub.add_parser(name, help="read-only health report")
        s.add_argument("--models", action="store_true", help="also check every repo id on Hugging Face")
        s.add_argument("--updates", action="store_true", help="also check PyPI for newer model-server packages")
        s.set_defaults(fn=cmd_status)

    s = sub.add_parser("models", help="list the model registry")
    s.add_argument("-v", "--verbose", action="store_true", help="also show repo, license, parsers")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("init-project", help="create the optional .local-agent/AGENT.md for project instructions")
    s.add_argument("dir", nargs="?", default=".", help="project folder (default: current folder)")
    s.set_defaults(fn=cmd_init_project)

    runcmds.add_parsers(sub)
    mediacmds.add_parsers(sub)
    agentcmds.add_parsers(sub)
    setupcmds.add_parsers(sub)
    backupcmds.add_parsers(sub)

    s = sub.add_parser("purge", help="remove v2 (used by install.sh --purge)")
    s.add_argument("--yes", action="store_true", help="required — confirms the removal")
    s.set_defaults(fn=cmd_purge)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = _translate_legacy(list(sys.argv[1:] if argv is None else argv))
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        err("interrupted — re-run ./install.sh; it continues from where it stopped")
        return 130
    except BrokenPipeError:                       # `law job 5 --md | head` — the reader left
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
