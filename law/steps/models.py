"""Models: download every `download: auto` model that isn't complete; prune models this
installer owns that are no longer in the registry (moved to ~/.law/trash, 7 days)."""
from __future__ import annotations

import shutil
import time

from ..engine import Action, Ctx, Step
from ..manifest import load_manifest
from ..registry import load
from ..util import read_env
from .v1 import cache_dir


class NotEnoughDisk(RuntimeError):
    pass


def is_complete(ctx: Ctx, repo: str) -> bool:
    d = cache_dir(ctx.paths, repo)
    snap = d / "snapshots"
    if not (snap.is_dir() and any(snap.iterdir())):
        return False
    if any((d / "blobs").glob("*.incomplete")):
        return False
    m = load_manifest(ctx.paths).model(repo)
    return m is None or bool(m.get("complete"))   # not ours → present is enough


def _empty_trash(ctx: Ctx) -> None:
    for d in ctx.paths.trash.iterdir() if ctx.paths.trash.is_dir() else []:
        shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink()


def _download(ctx: Ctx, repo: str, gb: float) -> None:
    need = gb * 1.05 + 2
    if ctx.sys.free_gb(ctx.paths.hf_cache) < need:
        _empty_trash(ctx)
        free = ctx.sys.free_gb(ctx.paths.hf_cache)
        if free < need:
            raise NotEnoughDisk(f"{repo} needs ~{need:.0f} GB free disk, only {free:.0f} GB available")
    man = load_manifest(ctx.paths)
    if man.model(repo) is None:
        man.own_model(repo, complete=False)
    ctx.sys.hf_download(ctx.paths.runtime / ".venv" / "bin" / "python", repo,
                        read_env(ctx.paths.env_file).get("HF_TOKEN"))
    load_manifest(ctx.paths).own_model(repo, complete=True)


def _prune(ctx: Ctx, repo: str) -> None:
    d = cache_dir(ctx.paths, repo)
    if d.exists():
        dest = ctx.paths.trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{d.name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest))          # same volume → instant rename
    load_manifest(ctx.paths).disown_model(repo)


def plan_models(ctx: Ctx) -> list[Action]:
    reg = load(ctx.paths)
    known = {m.repo for m in reg.models.values()}
    acts: list[Action] = []
    for m in reg.models.values():
        if m.download != "auto" or is_complete(ctx, m.repo):
            continue
        acts.append(Action("add", f"model {m.id}  ({m.repo}, ~{m.weights_gb:g} GB)",
                           lambda r=m.repo, g=m.weights_gb: _download(ctx, r, g)))
    for repo in list(load_manifest(ctx.paths).data["models"]):
        if repo not in known:
            acts.append(Action("remove", f"model {repo} (no longer in the registry → trash)",
                               lambda r=repo: _prune(ctx, r)))
    return acts


STEPS = [Step("models", "Models (download missing, prune unused)", plan_models, macos_only=True)]
