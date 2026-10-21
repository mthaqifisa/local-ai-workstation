"""`law api`: the real public API of a library symbol for the project in the current folder, in one command.

Coding agents guess library APIs from memory; when a guess is wrong the compiler says only "cannot find symbol",
and the agent either guesses again or digs through package internals for hours (seen in the lan-chat runs). Each
ecosystem module answers from the project's exact dependency versions, or, when a name does not exist, lists the
closest real names. The nearest project marker at or above the folder picks the ecosystem; `lang` overrides it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from law.apilookup import golang, java, monkeyc, swift, typescript
from law.apilookup.common import ApiLookupError

__all__ = ["LANGS", "ApiLookupError", "detect", "lookup"]

Lookup = Callable[..., str]
LANGS: dict[str, tuple[tuple[str, ...], Lookup]] = {
    "java": (java.MARKERS, java.lookup),
    "go": (golang.MARKERS, golang.lookup),
    "ts": (typescript.MARKERS, typescript.lookup),
    "swift": (swift.MARKERS, swift.lookup),
    "monkeyc": (monkeyc.MARKERS, monkeyc.lookup),
}


def detect(start: Path) -> str:
    """The ecosystem of the nearest project marker at or above `start` (the innermost project wins)."""
    for d in (start.resolve(), *start.resolve().parents):
        for lang, (markers, _) in LANGS.items():
            if any(any(d.glob(m)) for m in markers):
                return lang
    known = ", ".join(sorted({m for markers, _ in LANGS.values() for m in markers}))
    raise ApiLookupError(f"no project at or above {start} (looked for {known}); pass --lang")


def lookup(query: str, member: str | None = None, *, start: Path, lang: str | None = None,
           java_home: str | None = None) -> str:
    name = lang or detect(start)
    if name not in LANGS:
        raise ApiLookupError(f"unknown language {name!r}; one of: {', '.join(LANGS)}")
    if name == "java":
        return java.lookup(query, member, start=start, java_home=java_home)
    return LANGS[name][1](query, member, start=start)
