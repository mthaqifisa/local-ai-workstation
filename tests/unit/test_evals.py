"""Deterministic scoring used by `law eval`."""

from __future__ import annotations

import pytest

from law.evals import deterministic, last_number


@pytest.mark.parametrize("text,expected", [
    ("The final value is:\n**39,356**", 39356.0),
    ("Result: $1,956.07.", 1956.07),
    ("Working...\n4.18\n", 4.18),
    ("about 8.8%", 8.8),
    ("no digits here", None),
    ("Answer: -12.5", -12.5),
])
def test_last_number(text, expected):
    assert last_number(text) == expected


def test_number_within_tolerance():
    case = {"number": {"value": 39356, "tolerance": 2}}
    assert deterministic(case, "…\n39,357")["number"]
    assert not deterministic(case, "…\n39,373")["number"]


def test_must_and_any_are_case_insensitive():
    case = {"must": ["LEFT JOIN", "rank"], "any": ["Alice", "alicia"]}
    checks = deterministic(case, "select ... left join ... RANK() ... alice")
    assert checks == {"must": True, "any": True}
    assert not deterministic({"must": ["COALESCE"]}, "select 1")["must"]
