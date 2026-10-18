"""Model-server Python runtime (~/.law/runtime/.venv).

requirements.in (from the registry) → requirements.lock (resolved once, re-resolved only
when the list changes or with --upgrade) → `uv pip sync` (installs missing, REMOVES
anything not in the lock). Drift = installed set ≠ lock."""
from __future__ import annotations

import hashlib
import re

from ..engine import Action, Ctx, Step
from ..registry import load
from ..util import atomic_write


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(text: str) -> set[str]:
    pins = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "==" not in line:
            continue
        name, ver = line.split("==", 1)
        pins.add(f"{_norm(name.split('[')[0])}=={ver.split(';')[0].strip()}")
    return pins


def plan_python(ctx: Ctx) -> list[Action]:
    p = ctx.paths
    cfg = load(p).python
    ver = str(cfg.get("version", "3.12"))
    venv = p.runtime / ".venv"
    py = venv / "bin" / "python"
    req_in, lock, stamp = p.runtime / "requirements.in", p.runtime / "requirements.lock", p.runtime / ".in.sha"
    body = "# generated from registry.yaml — do not edit\n" + "\n".join(cfg.get("runtime", [])) + "\n"
    digest = hashlib.sha256(body.encode()).hexdigest()

    acts: list[Action] = []
    fresh = not ctx.sys.venv_python_ok(venv, ver)
    if fresh:
        acts.append(Action("add", f"Python {ver} venv {venv}", lambda: ctx.sys.venv_create(venv, ver)))
    relock = (fresh or (ctx.upgrade and "python" not in ctx.upgraded) or not lock.exists()
              or not stamp.exists() or stamp.read_text().strip() != digest)
    if relock:
        def compile_():
            atomic_write(req_in, body)
            ctx.sys.uv_compile(req_in, lock, py)
            atomic_write(stamp, digest + "\n")
            ctx.upgraded.add("python")
        acts.append(Action("update", "resolve runtime packages → requirements.lock", compile_))
    if relock or parse_pins(ctx.sys.uv_freeze(py)) != parse_pins(lock.read_text()):
        acts.append(Action("update", "sync runtime packages (install missing, remove unlisted)",
                           lambda: ctx.sys.uv_sync(lock, py)))
    return acts


STEPS = [Step("python", "Model-server Python runtime", plan_python, macos_only=True)]
