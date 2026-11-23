"""scripts/judge_calibration.py: the metrics and the brief's switch rule."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def jc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("judge_calibration", REPO / "scripts" / "judge_calibration.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _row(rep: int, item: str, kind: str, passed: bool, seconds: float = 10.0, error: str | None = None) -> dict:
    return {"repeat": rep, "id": item, "kind": kind, "score": 5 if passed else 2, "passed": passed,
            "seconds": seconds, "error": error}


def test_metrics(jc: ModuleType) -> None:
    rows = [_row(0, "a/good", "good", True), _row(1, "a/good", "good", False),  # disagrees across runs
            _row(0, "a/factual", "factual", False), _row(1, "a/factual", "factual", False),
            _row(0, "a/omission", "omission", True, 30.0), _row(1, "a/omission", "omission", True),
            _row(2, "a/omission", "omission", False, error="ReadTimeout")]  # errors don't count
    m = jc.metrics(rows)
    assert m == {"catch_pct": 50.0, "false_fail_pct": 50.0, "agreement_pct": 66.7, "median_s": 10.0, "errors": 1.0}


@pytest.mark.parametrize(("cand", "ok"), [
    ({"catch_pct": 80, "false_fail_pct": 10, "agreement_pct": 90, "median_s": 45}, True),
    ({"catch_pct": 79, "false_fail_pct": 10, "agreement_pct": 90, "median_s": 45}, False),  # < +10 points
    ({"catch_pct": 90, "false_fail_pct": 20, "agreement_pct": 90, "median_s": 45}, False),  # more false fails
    ({"catch_pct": 90, "false_fail_pct": 10, "agreement_pct": 80, "median_s": 45}, False),  # less agreement
    ({"catch_pct": 90, "false_fail_pct": 10, "agreement_pct": 90, "median_s": 46}, False),  # > 1.5x time
])
def test_switch_rule(jc: ModuleType, cand: dict, ok: bool) -> None:
    current = {"catch_pct": 70, "false_fail_pct": 10, "agreement_pct": 90, "median_s": 30}
    assert jc.switch(cand, current) is ok
