"""Named agent instructions, resolved when a step runs, not when its pipeline is created.

A pipeline stage carries `spec_ref` / `rubric_ref` (a name) and `spec_args` (the values to fill in) instead of the
instruction text. Each step looks the text up at run time, so a retried, restarted or long-running pipeline always
works to the current rules. (Seen 2026-10-20: a proposal created at 17:49 kept running on old instructions all
evening, missing rules added since.) Older stages that still carry `spec` / `rubric` text keep working.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Builder = Callable[[dict[str, Any]], str]
SPECS: dict[str, Builder] = {}


def register(name: str) -> Callable[[Builder], Builder]:
    def wrap(fn: Builder) -> Builder:
        SPECS[name] = fn
        return fn
    return wrap


def resolve(payload: dict[str, Any], kind: str = "spec") -> str | None:
    """The stage's `spec` (or `rubric`): built now from `<kind>_ref`, else the stored text, else None."""
    ref = payload.get(f"{kind}_ref")
    if ref:
        if ref not in SPECS:
            raise KeyError(f"unknown {kind} {ref!r}")
        return SPECS[ref](payload.get("spec_args") or {})
    text = payload.get(kind)
    return str(text) if text else None
