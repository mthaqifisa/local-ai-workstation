"""Folders, secrets file, generated-file pruning, trash, version stamp."""
from __future__ import annotations

import json
import os
import shutil
import stat
import time

from .. import __version__
from ..engine import Action, Ctx, Step
from ..registry import load
from ..util import atomic_write, merge_env

TRASH_DAYS = 7


def _dirs(ctx: Ctx):
    p = ctx.paths
    return [p.law, p.config, p.state, p.run, p.logs, p.backups, p.generated, p.trash,
            p.workspace, p.source, p.outputs]


def plan_dirs(ctx: Ctx) -> list[Action]:
    missing = [d for d in _dirs(ctx) if not d.is_dir()]
    if not missing:
        return []
    return [Action("add", ", ".join(str(d) for d in missing),
                   lambda: [d.mkdir(parents=True, exist_ok=True) for d in missing])]


def plan_env(ctx: Ctx) -> list[Action]:
    f = ctx.paths.env_file
    if not f.exists():
        return [Action("add", f"{f} (chmod 600)", lambda: merge_env(f, {}))]
    if stat.S_IMODE(f.stat().st_mode) != 0o600:
        return [Action("update", f"{f} permissions → 600", lambda: os.chmod(f, 0o600))]
    return []


# ── generated files: write expected, delete anything else ───────────────
def expected_generated(ctx: Ctx) -> dict[str, str]:
    reg = load(ctx.paths)
    resolved = {"ports": reg.ports, "max_weights_gb": reg.max_weights_gb,
                "models": {m.id: dict(vars(m)) for m in reg.models.values()},
                "personas": reg.personas, "defaults": reg.raw.get("defaults", {}),
                "runtime": reg.raw.get("runtime", {})}
    return {"registry.resolved.json": json.dumps(resolved, indent=2, sort_keys=True) + "\n"}


def plan_generated(ctx: Ctx) -> list[Action]:
    g = ctx.paths.generated
    want = expected_generated(ctx)
    acts = []
    for name, body in want.items():
        f = g / name
        if not f.exists() or f.read_text() != body:
            acts.append(Action("update" if f.exists() else "add", f"generated/{name}",
                               lambda f=f, body=body: atomic_write(f, body)))
    if g.is_dir():
        for f in g.iterdir():
            if f.name not in want:
                acts.append(Action("remove", f"generated/{f.name} (no longer used)",
                                   lambda f=f: shutil.rmtree(f) if f.is_dir() else f.unlink()))
    return acts


# ── trash: things removed by the installer wait here 7 days ─────────────
def plan_trash(ctx: Ctx) -> list[Action]:
    t = ctx.paths.trash
    if not t.is_dir():
        return []
    cutoff = time.time() - TRASH_DAYS * 86400
    old = [d for d in t.iterdir() if d.stat().st_mtime < cutoff]
    return [Action("remove", f"trash older than {TRASH_DAYS} days: {d.name}",
                   lambda d=d: shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink())
            for d in old]


def plan_version(ctx: Ctx) -> list[Action]:
    f = ctx.paths.state / "version"
    if f.exists() and f.read_text().strip() == __version__:
        return []
    return [Action("update", f"version → {__version__}", lambda: atomic_write(f, __version__ + "\n"))]


FIRST = [
    Step("dirs", "Folders (~/.law, ~/MLX-AI)", plan_dirs),
    Step("env", "Secrets file (~/.law/.env)", plan_env),
]
LAST = [
    Step("generated", "Generated files", plan_generated),
    Step("trash", f"Trash (removed items kept {TRASH_DAYS} days)", plan_trash),
    Step("version", "Version stamp", plan_version),
]
