"""Reconcile engine: every step is plan -> apply -> verify; the whole run holds a lock.

Re-run guarantees (ADR-0008): a run with nothing to do changes nothing; an interrupted run
converges on the next run because each step re-plans from the real machine state.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rich.console import Console

from law.paths import LawPaths
from law.sysapi import SysAPI


class Step(Protocol):
    name: str

    def plan(self) -> list[str]:
        """Human-readable changes needed. Empty list = already in the desired state."""

    def apply(self) -> None:
        """Make the changes. Must be safe to repeat."""

    def verify(self) -> list[str]:
        """Problems remaining after apply. Empty list = healthy."""


@dataclass
class StepReport:
    name: str
    planned: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0


@dataclass
class RunReport:
    steps: list[StepReport]
    dry_run: bool

    @property
    def changes(self) -> int:
        return sum(len(s.planned) for s in self.steps)

    @property
    def ok(self) -> bool:
        return all(not s.problems and not s.error for s in self.steps)


class LockedError(RuntimeError):
    pass


@contextmanager
def install_lock(paths: LawPaths) -> Iterator[None]:
    paths.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.state / "install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockedError("another `law install` is running") from exc
        yield
    finally:
        os.close(fd)


def run(paths: LawPaths, steps: list[Step], *, dry_run: bool, console: Console | None = None,
        only: set[str] | None = None) -> RunReport:
    con = console or Console()
    reports: list[StepReport] = []
    with install_lock(paths):
        for step in steps:
            if only and step.name not in only:
                continue
            rep = StepReport(step.name)
            t0 = time.monotonic()
            try:
                rep.planned = step.plan()
                if rep.planned:
                    for change in rep.planned:
                        con.print(f"[cyan]{step.name}[/]: {change}")
                    if not dry_run:
                        step.apply()
                        rep.problems = step.verify()
                elif not dry_run:
                    rep.problems = step.verify()
                status = "[green]ok[/]" if not rep.problems else f"[red]{'; '.join(rep.problems)}[/]"
                if not rep.planned:
                    con.print(f"[dim]{step.name}: in sync[/] {status if rep.problems else ''}")
                elif not dry_run:
                    con.print(f"{step.name}: {status}")
            except Exception as exc:
                rep.error = f"{type(exc).__name__}: {exc}"
                con.print(f"[red]{step.name} failed: {rep.error}[/]")
                rep.seconds = time.monotonic() - t0
                reports.append(rep)
                break  # later steps depend on earlier ones; next run resumes here
            rep.seconds = time.monotonic() - t0
            reports.append(rep)
    report = RunReport(reports, dry_run)
    if not dry_run:
        _record(paths, report)
    return report


def _record(paths: LawPaths, report: RunReport) -> None:
    state_file: Path = paths.state / "install.json"
    state_file.write_text(json.dumps({
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ok": report.ok,
        "changes": report.changes,
        "steps": [{"name": s.name, "planned": s.planned, "problems": s.problems, "error": s.error,
                   "seconds": round(s.seconds, 2)} for s in report.steps],
    }, indent=2))


def owned(paths: LawPaths) -> dict[str, list[str]]:
    f = paths.state / "owned.json"
    return json.loads(f.read_text()) if f.exists() else {}


def record_owned(paths: LawPaths, kind: str, item: str, sysapi: SysAPI | None = None) -> None:
    """Remember what LAW installed so uninstall removes only that."""
    data = owned(paths)
    items = set(data.get(kind, []))
    if item not in items:
        items.add(item)
        data[kind] = sorted(items)
        (paths.state / "owned.json").write_text(json.dumps(data, indent=2))
