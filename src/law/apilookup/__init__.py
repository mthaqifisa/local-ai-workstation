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

__all__ = ["LANGS", "ApiLookupError", "below", "detect", "locate", "lookup"]

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


def below(start: Path) -> list[tuple[str, Path]]:
    """Projects one level below `start` (a repo root holding e.g. api/pom.xml and ui/package.json)."""
    found: list[tuple[str, Path]] = []
    for d in sorted(p for p in start.resolve().iterdir() if p.is_dir() and not p.name.startswith(".")):
        for lang, (markers, _) in LANGS.items():
            if any(any(d.glob(m)) for m in markers):
                found.append((lang, d))
                break
    return found


def locate(start: Path, lang: str | None = None) -> tuple[str, Path]:
    """The ecosystem and the folder to answer from: the nearest project at or above `start`, else the one below it
    (seen 2026-10-25: every lookup from a repo root with api/pom.xml failed, and the agent fell back to guessing)."""
    if lang is not None:
        if lang not in LANGS:
            raise ApiLookupError(f"unknown language {lang!r}; one of: {', '.join(LANGS)}")
        try:
            if detect(start) == lang:
                return lang, start
        except ApiLookupError:
            pass
        dirs = [d for name, d in below(start) if name == lang]
        return lang, dirs[0] if len(dirs) == 1 else start
    try:
        return detect(start), start
    except ApiLookupError:
        subs = below(start)
        if len(subs) == 1:
            return subs[0]
        if subs:
            listed = ", ".join(f"{d.name}/ ({name})" for name, d in subs)
            raise ApiLookupError(f"several projects below {start}: {listed}; run law api in that folder "
                                 "or pass --lang") from None
        raise


def lookup(query: str, member: str | None = None, *, start: Path, lang: str | None = None,
           java_home: str | None = None) -> str:
    name, where = locate(start, lang)
    if name == "java":
        return java.lookup(query, member, start=where, java_home=java_home)
    return LANGS[name][1](query, member, start=where)
