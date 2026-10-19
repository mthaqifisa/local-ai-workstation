"""`law eval`: run the golden set through the real product and score it.

Every case goes through the job API exactly as a user request would (quality pipeline included).
Scoring is deterministic first (numbers, required phrases); cases marked `judge` are then scored by
the independent reviewer model on the *final* answer. Reports are generated from raw results only.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from law import client
from law.handlers.core import RUBRIC, parse_verdict

NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


@dataclass
class CaseResult:
    id: str
    domain: str
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    judge_score: int | None = None
    judge_summary: str = ""
    seconds: float = 0.0
    answer: str = ""
    error: str | None = None


def last_number(text: str) -> float | None:
    """The last number in the final non-empty line(s) of an answer (commas and currency stripped)."""
    for line in reversed([ln for ln in text.strip().splitlines() if ln.strip()]):
        nums = NUM_RE.findall(line.replace("$", ""))
        if nums:
            try:
                return float(nums[-1].replace(",", "").rstrip("."))
            except ValueError:
                continue
    return None


def deterministic(case: dict[str, Any], answer: str) -> dict[str, bool]:
    low = answer.lower()
    checks: dict[str, bool] = {}
    if "number" in case:
        got = last_number(answer)
        spec = case["number"]
        checks["number"] = got is not None and abs(got - float(spec["value"])) <= float(spec["tolerance"])
    if "must" in case:
        checks["must"] = all(m.lower() in low for m in case["must"])
    if "any" in case:
        checks["any"] = any(a.lower() in low for a in case["any"])
    return checks


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def run(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = client.run(self.base, kind, payload, source="eval", priority=2)
        if job["status"] != "done":
            raise RuntimeError(f"{kind} job {job['id']} {job['status']}: {job.get('error')}")
        result: dict[str, Any] = job["result"]
        return result


def run_case(client: Client, case: dict[str, Any], pass_score: int) -> CaseResult:
    res = CaseResult(case["id"], case["domain"], passed=False)
    t0 = time.monotonic()
    try:
        out = client.run("ask", {"prompt": case["prompt"], "role": case.get("role", "assistant"), "quality": True})
        res.answer = str(out.get("answer", ""))
        res.checks = deterministic(case, res.answer)
        if case.get("judge"):
            text = client.run("ask", {"role": "reviewer", "quality": False,
                                      "prompt": f"{RUBRIC}\n\nREQUEST:\n{case['prompt']}\n\nRESPONSE:\n{res.answer}"})
            verdict = parse_verdict(str(text.get("answer", "")))
            res.judge_score = verdict["score"]
            res.judge_summary = verdict["summary"]
            res.checks["judge"] = res.judge_score >= pass_score
        res.passed = bool(res.checks) and all(res.checks.values())
    except Exception as exc:  # report, don't abort the suite
        res.error = f"{type(exc).__name__}: {exc}"
    res.seconds = round(time.monotonic() - t0, 1)
    return res


def run_suite(golden: Path, base: str, out_dir: Path, only: set[str] | None = None) -> list[CaseResult]:
    spec = yaml.safe_load(golden.read_text())
    client = Client(base)
    results = [run_case(client, c, int(spec.get("pass_score", 4))) for c in spec["cases"]
               if not only or c["id"] in only or c["domain"] in only]
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out_dir / f"eval-{stamp}.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    return results


def markdown(results: list[CaseResult]) -> str:
    passed = sum(r.passed for r in results)
    lines = [f"**{passed}/{len(results)} passed**", "",
             "| Case | Domain | Result | Checks | Judge | Time (s) |", "|---|---|---|---|---|---|"]
    for r in results:
        checks = ", ".join(f"{k} {'✓' if v else '✗'}" for k, v in r.checks.items()) or (r.error or "")
        judge = f"{r.judge_score}/5" if r.judge_score is not None else "n/a"
        lines.append(f"| {r.id} | {r.domain} | {'PASS' if r.passed else 'FAIL'} | {checks} | {judge} | {r.seconds} |")
    return "\n".join(lines)
