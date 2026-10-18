"""Reconcile engine: every step compares DESIRED state with ACTUAL state.

    plan(ctx) -> [Action, ...]     what must change (add / update / remove)
    empty plan                     → step already converged, nothing happens
    non-empty plan                 → run the actions, then plan AGAIN until it is empty

So every run: installs what's missing, repairs what drifted, removes what's no longer
needed — and a second run changes nothing. Removal only ever touches things this
installer owns (see manifest.py) or the retired v1 stack.
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .paths import Paths
from .util import atomic_write, err, head, ok, skip, warn

SYM = {"add": "+", "update": "~", "remove": "-"}
MAX_ROUNDS = 3


@dataclass
class Action:
    kind: str                       # add | update | remove
    desc: str
    fn: Callable[[], None]


@dataclass
class Ctx:
    paths: Paths
    sys: Any                        # law.system.System (or a fake in tests)
    dry_run: bool = False
    upgrade: bool = False
    keep_v1: bool = False
    src: str | None = None
    upgraded: set = field(default_factory=set)   # steps that already did their --upgrade


@dataclass
class Step:
    name: str
    title: str
    plan: Callable[[Ctx], list[Action]]
    required: bool = True           # False → failure is a warning, run continues
    macos_only: bool = False


class StepFailed(RuntimeError):
    pass


@dataclass
class Report:
    results: dict[str, str] = field(default_factory=dict)
    added: int = 0
    updated: int = 0
    removed: int = 0


def _record(ctx: Ctx, name: str, **kw) -> None:
    if ctx.dry_run:
        return
    p = ctx.paths.install_state
    try:
        state = json.loads(p.read_text()) if p.exists() else {}
    except json.JSONDecodeError:
        state = {}
    state.setdefault("steps", {}).setdefault(name, {}).update(kw, at=time.time())
    atomic_write(p, json.dumps(state, indent=2, sort_keys=True))


def _log_error(ctx: Ctx, name: str) -> str:
    logf = ctx.paths.logs / "install-error.log"
    try:
        logf.parent.mkdir(parents=True, exist_ok=True)
        with logf.open("a") as f:
            f.write(f"\n--- {time.ctime()} {name}\n{traceback.format_exc()}")
    except OSError:
        pass
    return str(logf)


def run_steps(ctx: Ctx, steps: list[Step], *, on_macos: bool) -> Report:
    rep = Report()
    for i, st in enumerate(steps, 1):
        head(f"[{i}/{len(steps)}] {st.title}")
        if st.macos_only and not on_macos:
            skip("not macOS — skipped"); rep.results[st.name] = "skipped"; continue
        try:
            actions = st.plan(ctx)
            if not actions:
                skip("up to date")
                rep.results[st.name] = "ok"
                _record(ctx, st.name, status="ok")
                continue
            if ctx.dry_run:
                for a in actions:
                    warn(f"would {SYM.get(a.kind, '?')} {a.desc}")
                rep.results[st.name] = "would-change"
                continue
            for _round in range(MAX_ROUNDS):          # some steps need 2 passes (e.g. secret → config)
                for a in actions:
                    print(f"  {SYM.get(a.kind, '?')} {a.desc}", flush=True)
                    a.fn()
                    rep.added += a.kind == "add"
                    rep.updated += a.kind == "update"
                    rep.removed += a.kind == "remove"
                actions = st.plan(ctx)                # verify: must converge
                if not actions:
                    break
            else:
                raise StepFailed("still not converged after changes: "
                                 + "; ".join(a.desc for a in actions))
            ok("done")
            rep.results[st.name] = "changed"
            _record(ctx, st.name, status="ok", error=None)
        except Exception as e:  # noqa: BLE001 — report every failure the same way
            logf = _log_error(ctx, st.name)
            _record(ctx, st.name, status="failed", error=str(e))
            if st.required:
                err(f"{st.title}: {e}")
                err(f"details: {logf}  ·  fix the cause, then re-run ./install.sh (it picks up from here)")
                rep.results[st.name] = "failed"
                raise StepFailed(st.name) from e
            warn(f"{st.title}: {e}  (optional — continuing)")
            rep.results[st.name] = "warn"
    return rep
