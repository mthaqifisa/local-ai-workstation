"""Ticket estimates and the project's expected finish (owner 2026-11-24)."""

from __future__ import annotations

import pytest

from law.board import plan_tickets
from law.estimate import OVERRUN_MIN_S, Item, forecast, parse

NOW = 1_800_000_000.0


@pytest.mark.parametrize(("text", "title", "seconds"), [
    ("Write §1.1 (acceptance: builds) [est 40m]", "Write §1.1 (acceptance: builds)", 2400),
    ("Split [est 1.5h] the file", "Split the file", 5400),
    ("Big one [Est: 2 days]", "Big one", 172800),
    ("No estimate", "No estimate", None),
    ("Zero [est 0m]", "Zero", None),
])
def test_parse(text: str, title: str, seconds: float | None) -> None:
    assert parse(text) == (title, seconds)


def test_plan_tickets_strip_the_tag() -> None:
    [t] = plan_tickets("### M1 A\n- [ ] K-1 Do it (acceptance: works) [est 30m]\n")
    assert t["est"] == 1800 and "[est" not in t["title"] and "[est" not in t["details"]


def test_raw_estimates_before_any_history() -> None:
    fc = forecast([Item("K-1", "progress", 600, started=NOW - 120), Item("K-2", "todo", 1200)], NOW)
    assert fc.pace == 1.0
    assert fc.tickets["K-1"] == {"expect": 600, "due": NOW + 480, "overrun": False}
    assert fc.due == NOW + 480 + 1200 and fc.remaining == 2


def test_pace_from_finished_tickets_scales_the_rest() -> None:
    items = [Item("K-1", "done", 600, NOW - 3000, NOW - 1800),  # took 1200 for 600: twice as long
             Item("K-2", "done", 600, NOW - 1800, NOW - 600),
             Item("K-3", "todo", 900)]
    fc = forecast(items, NOW)
    assert fc.pace == 2.0 and fc.samples == 2
    assert fc.tickets["K-3"]["expect"] == 1800 and fc.due == NOW + 1800


def test_overrun_gets_a_new_due_time() -> None:
    fc = forecast([Item("K-1", "progress", 600, started=NOW - 4000)], NOW)
    assert fc.tickets["K-1"]["overrun"] is True
    assert fc.tickets["K-1"]["due"] == NOW + 1000  # a quarter of the 4000 s taken
    short = forecast([Item("K-1", "progress", 60, started=NOW - 100)], NOW)
    assert short.tickets["K-1"]["due"] == NOW + OVERRUN_MIN_S


def test_no_estimate_uses_the_typical_finished_time() -> None:
    items = [Item("K-1", "done", None, NOW - 900, NOW - 300), Item("K-2", "todo")]
    fc = forecast(items, NOW)
    assert fc.tickets["K-2"]["expect"] == 600 and fc.unknown == 0


def test_nothing_to_go_on() -> None:
    fc = forecast([Item("K-1", "todo")], NOW)
    assert fc.due is None and fc.unknown == 1 and fc.project(NOW) is None


def test_pace_is_clamped() -> None:
    items = [Item("K-1", "done", 60, NOW - 100_000, NOW - 50_000), Item("K-2", "done", 60, NOW - 50_000, NOW)]
    assert forecast(items, NOW).pace == 6.0
