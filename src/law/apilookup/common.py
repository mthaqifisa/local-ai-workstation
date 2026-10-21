"""Shared pieces of `law api`: the error type, project discovery, close-name suggestions and output limits."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from pathlib import Path

MAX_LINES = 200


class ApiLookupError(Exception):
    """A lookup that cannot run (no supported project, dependencies not resolvable, a tool missing)."""


def find_up(start: Path, markers: Iterable[str]) -> Path | None:
    """The nearest directory at or above `start` that holds one of `markers` (file names or glob patterns)."""
    pats = list(markers)
    for d in (start.resolve(), *start.resolve().parents):
        if any(any(d.glob(p)) for p in pats):
            return d
    return None


def close_names(query: str, names: Iterable[str], limit: int = 8) -> list[str]:
    """Real names close to one that does not exist: similar spelling first, then a shared prefix."""
    pool = sorted(set(names))
    close = difflib.get_close_matches(query, pool, n=limit, cutoff=0.6)
    lower = query.lower()
    prefix = [n for n in pool if len(query) >= 4 and n.lower().startswith(lower[:4])]
    return list(dict.fromkeys([*close, *prefix]))[:limit]


def not_found(kind: str, query: str, where: str, near: list[str]) -> str:
    """The answer for a name that does not exist: say so plainly, then offer the closest real names."""
    hint = ("\nClosest real names:\n" + "\n".join(f"  {n}" for n in near)) if near else ""
    return (f"No {kind} named '{query}' in {where}. It does not exist in these versions: do not use it.{hint}")


def clip(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= MAX_LINES:
        return text
    return "\n".join([*lines[:MAX_LINES], f"... {len(lines) - MAX_LINES} more lines: add a member filter"])


def filter_lines(text: str, member: str, *, keep: int = 1) -> str:
    """The first `keep` lines (the declaration header) plus the lines that mention `member` (case-insensitive)."""
    lines = text.splitlines()
    head, body = lines[:keep], lines[keep:]
    hits = [ln for ln in body if member.lower() in ln.lower()]
    return "\n".join([*head, *(hits or [f"  (no member mentions '{member}')"])])
