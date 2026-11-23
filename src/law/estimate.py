"""Ticket time estimates and the project's expected finish (owner 2026-11-24).

The planner writes an estimate on every ticket (`[est 45m]`, `[est 2h]`, `[est 1d]`). Raw model estimates are often
off by a large factor, so LAW calibrates them with this project's own record: the pace is the time the finished
tickets really took divided by what was estimated for them, and it scales every open estimate. A ticket without an
estimate counts as the median time of the finished ones. A running ticket that passes its due time gets a new due
time from the time it has already taken, so the countdown never sits at zero.

Tickets run one after another (one build session per project), so the project's finish is the running ticket's
due time plus the calibrated estimates of every ticket still to do.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

EST = re.compile(r"\s*\[\s*est(?:imate)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)\s*\]",
                 re.IGNORECASE)
UNIT_S = {"m": 60, "h": 3600, "d": 86400}
PACE_MIN, PACE_MAX = 0.25, 6.0
PACE_SAMPLES = 2  # finished tickets with an estimate before the pace replaces 1.0
OVERRUN_EXTRA = 0.25  # an overrunning ticket is given a quarter of its elapsed time more ...
OVERRUN_MIN_S = 300  # ... and at least five minutes


def parse(text: str) -> tuple[str, float | None]:
    """The text without its `[est …]` tag, and the estimate in seconds (None when there is none)."""
    m = EST.search(text)
    if not m:
        return text, None
    seconds = float(m.group(1)) * UNIT_S[m.group(2)[0].lower()]
    return (text[:m.start()] + text[m.end():]).strip(), seconds if seconds > 0 else None


@dataclass
class Item:
    """One ticket as the forecast sees it."""
    id: str
    column: str  # todo, progress, done, ...
    est: float | None = None
    started: float | None = None
    finished: float | None = None


@dataclass
class Forecast:
    pace: float = 1.0
    samples: int = 0
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> {"expect", "due", "overrun"}
    due: float | None = None  # the project's expected finish (open dev tickets)
    remaining: int = 0  # open tickets counted
    unknown: int = 0  # open tickets with no estimate and no history to stand in for one

    def project(self, now: float) -> dict[str, Any] | None:
        if self.due is None:
            return None
        return {"due": self.due, "left_s": max(0.0, self.due - now), "pace": round(self.pace, 2),
                "samples": self.samples, "remaining": self.remaining, "unknown": self.unknown}


def forecast(items: list[Item], now: float) -> Forecast:
    spans = [(i, i.finished - i.started) for i in items  # finished tickets and the time they really took
             if i.column == "done" and i.started and i.finished and i.finished > i.started]
    timed = [(i.est, took) for i, took in spans if i.est]
    fc = Forecast()
    if len(timed) >= PACE_SAMPLES:
        fc.pace = min(PACE_MAX, max(PACE_MIN, sum(t for _, t in timed) / sum(e for e, _ in timed)))
        fc.samples = len(timed)
    typical = statistics.median(took for _, took in spans) if spans else None

    def expect(i: Item) -> float | None:
        return i.est * fc.pace if i.est else typical

    clock = now
    opened = False
    for i in items:
        if i.column == "progress":
            span = expect(i)
            if span is None:
                fc.unknown += 1
                continue
            start = i.started or now
            elapsed = max(0.0, now - start)
            overrun = elapsed >= span
            due = now + max(OVERRUN_MIN_S, elapsed * OVERRUN_EXTRA) if overrun else start + span
            fc.tickets[i.id] = {"expect": span, "due": due, "overrun": overrun}
            clock, opened = max(clock, due), True
            fc.remaining += 1
    for i in items:
        if i.column == "todo":
            span = expect(i)
            if span is None:
                fc.unknown += 1
                continue
            fc.tickets[i.id] = {"expect": span}
            clock += span
            opened = True
            fc.remaining += 1
    fc.due = clock if opened else None
    return fc
