"""Keep OpenCode's "law" provider in sync with the registry (optional step).

Runs only when OpenCode is installed or ~/.config/opencode exists, and when
runtime.opencode.manage is true. Never fails the install: a problem is a warning.
"""
from __future__ import annotations

from .. import opencode
from ..engine import Action, Ctx, Step
from ..registry import load


def plan_opencode(ctx: Ctx) -> list[Action]:
    reg = load(ctx.paths)
    if not ((reg.raw.get("runtime") or {}).get("opencode") or {}).get("manage", True):
        return []
    home = ctx.paths.home
    if not opencode.in_use(home):
        return []
    try:
        changed, want = opencode.needs_update(home, reg)
    except opencode.JsoncConfig:
        return []                       # user keeps a commented config — `law setup-opencode` explains
    if not changed:
        return []
    f = opencode.config_file(home)
    return [Action("update" if f.exists() else "add", f"OpenCode provider 'law' in {f}",
                   lambda: opencode.write(home, want))]


STEPS = [Step("opencode", "OpenCode → law gateway", plan_opencode, required=False)]
