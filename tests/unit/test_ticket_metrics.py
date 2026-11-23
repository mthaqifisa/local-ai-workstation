"""scripts/ticket_metrics.py: ticket sessions from a job's events."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tm() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_metrics", REPO / "scripts" / "ticket_metrics.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def ev(t: float, msg: str = "", event: str = "progress") -> dict:
    return {"t": t, "event": event, "data": {"message": msg}}


def test_two_ticket_sessions(tm: ModuleType) -> None:
    events = [ev(0, event="started"), ev(60, "coder agent working in ~/SourceCode/pantry"),
              ev(600, "📖 read a.ts"), ev(3000, "✏️ edit api/a.ts"), ev(3600, "⚠️ nothing saved for 25 min"),
              ev(6000, '▶ git commit -m "P-37.1 lookup chain"'),
              ev(6060, "coder agent working in ~/SourceCode/pantry"), ev(7000, "✏️ write b.ts"),
              ev(9000, "▶ git commit -m 'P-37.2 cache'"), ev(9100, '▶ git commit -m "P-37.2 fix"')]
    a, b = tm.tickets(list(reversed(events)))  # order of the input does not matter
    assert (a.ticket, a.minutes(a.first_save), a.nudges, a.minutes(a.commit)) == ("P-37.1", 50, 1, 100)
    assert (b.ticket, b.minutes(b.first_save), b.nudges, b.minutes(b.commit)) == ("P-37.2", 16, 0, 49)
    table = tm.render([a, b])
    assert "| P-37.1 |" in table and "| 50 | 1 | 100 |" in table


def test_unfinished_session(tm: ModuleType) -> None:
    (only,) = tm.tickets([ev(0, event="started"), ev(120, "📖 read x")])
    assert only.ticket is None and only.first_save is None and "| ? |" in tm.render([only])
    assert "| - | 0 | - |" in tm.render([only])
