#!/usr/bin/env python3
"""Judge calibration (ENHANCE brief E5, ADR-0007): does the judge catch seeded flaws without failing good answers?

    judge_calibration.py build --eval <golden eval JSON>   # writes trained/evals/judge-calibration.yaml
    judge_calibration.py run --judge gemma-4-31b-8bit [--repeats 3]
    judge_calibration.py report                            # table from every run in ~/LAW/outputs/judge/

The set: each golden case's known-good answer from a passing eval, plus two flawed variants written by the 27B (one
factual or numeric error; one omission of a required point or a broken code path), each with its flaw stated so a
person can check it. Runs go through LAW's own reviewer path (`handlers.core.answer` with the reviewer role: same
system prompt, rubric, calculator tool, reasoning effort and the model card's sampling), with only the model swapped.
Metrics: catch rate (flawed items scored below the pass score), false-fail rate (good items below it), run-to-run
agreement (items with the same pass/fail in every run) and median seconds per verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from law.handlers.base import Runtime  # noqa: E402 - the repo's own package, importable once src/ is on the path
from law.handlers.core import RUBRIC, answer, parse_verdict  # noqa: E402
from law.paths import LawPaths  # noqa: E402
from law.privacy import scrub  # noqa: E402

SET_FILE = REPO / "trained" / "evals" / "judge-calibration.yaml"
GOLDEN = REPO / "trained" / "evals" / "golden.yaml"
FLAWS = {
    "factual": "Introduce exactly ONE factual or numeric error that a careful expert would catch (a wrong figure, a "
               "wrong final number, a wrong fact or a wrong conclusion), and keep everything else identical.",
    "omission": "Remove or break exactly ONE thing the request requires (a required section, point or table; for "
                "code, a broken code path or a missing required behaviour such as the error case), keep everything "
                "else identical, and do not mention the removal.",
}
SEED = """You prepare test material for calibrating an AI judge. Below are a REQUEST and a known-good RESPONSE.
{instruction}
Reply with JSON only: {{"flaw": "<one sentence stating exactly what you changed and why it is wrong>",
"response": "<the full modified response>"}}

REQUEST:
{prompt}

RESPONSE:
{response}"""


def golden_judged() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in yaml.safe_load(GOLDEN.read_text())["cases"]}


async def build(eval_rows: list[dict[str, Any]], model: str) -> None:
    rt = Runtime.load(LawPaths.from_env())
    cases = golden_judged()
    good = [r for r in eval_rows if r["passed"] and r["id"] in cases]
    items: list[dict[str, Any]] = []
    for r in good:
        case = cases[r["id"]]
        items.append({"id": f"{r['id']}/good", "case": r["id"], "kind": "good", "flaw": "",
                      "prompt": case["prompt"], "response": r["answer"]})
        for kind, instruction in FLAWS.items():
            text = await rt.llm.chat(model, [{"role": "user", "content": SEED.format(
                instruction=instruction, prompt=case["prompt"], response=r["answer"])}],
                temperature=0.7, max_tokens=16000, reasoning="off")
            seeded = json.loads(text[text.find("{"):text.rfind("}") + 1])
            items.append({"id": f"{r['id']}/{kind}", "case": r["id"], "kind": kind, "flaw": seeded["flaw"],
                          "prompt": case["prompt"], "response": seeded["response"]})
            print(f"{r['id']}/{kind}: {seeded['flaw']}", flush=True)
    header = ("# Judge calibration set (ADR-0007, built by scripts/judge_calibration.py build). Good answers are from "
              "a passing golden eval;\n# each flawed variant states its seeded flaw. Checked by hand against each "
              "case's request before use.\n")
    SET_FILE.write_text(scrub(header + dump({"version": 1, "items": items})))


class _Literal(yaml.SafeDumper):
    """Multi-line strings as `|` blocks: readable, and no escaped `\\n@decorator` that looks like an address."""


_Literal.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s
                                                               else None))


def dump(data: dict[str, Any]) -> str:
    for item in data.get("items", []):  # a trailing space would force a quoted (escaped) string
        for key in ("prompt", "response", "flaw"):
            text = str(item[key])
            item[key] = "\n".join(line.rstrip() for line in text.splitlines()) + ("\n" if "\n" in text else "")
    return yaml.dump(data, Dumper=_Literal, sort_keys=False, allow_unicode=True, width=120)


async def run(judge: str, repeats: int, pass_score: int) -> Path:
    rt = Runtime.load(LawPaths.from_env())
    items = yaml.safe_load(SET_FILE.read_text())["items"]
    rows = []
    for rep in range(repeats):
        for item in items:
            prompt = f"{RUBRIC}\n\nREQUEST:\n{item['prompt']}\n\nRESPONSE:\n{item['response']}"
            messages = [{"role": "system", "content": rt.roles.system_prompt("reviewer")},
                        {"role": "user", "content": prompt}]
            t0 = time.monotonic()
            try:
                verdict = parse_verdict(await answer(rt, "reviewer", messages, model=judge))
                score, error = int(verdict["score"]), None
            except Exception as exc:  # a failed verdict is recorded, not fatal
                score, error = 0, f"{type(exc).__name__}: {exc}"
            rows.append({"repeat": rep, "id": item["id"], "kind": item["kind"], "score": score,
                         "passed": score >= pass_score, "seconds": round(time.monotonic() - t0, 1), "error": error})
            print(f"{judge} r{rep} {item['id']}: {score}", flush=True)
    out = LawPaths.from_env().outputs / "judge" / f"judge-{time.strftime('%Y%m%d-%H%M%S')}-{judge}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"judge": judge, "pass_score": pass_score, "rows": rows}, indent=1))
    return out


def metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in rows if not r["error"]]
    flawed = [r for r in ok if r["kind"] != "good"]
    good = [r for r in ok if r["kind"] == "good"]
    by_item: dict[str, set[bool]] = {}
    for r in ok:
        by_item.setdefault(r["id"], set()).add(r["passed"])
    return {"catch_pct": round(100 * sum(not r["passed"] for r in flawed) / len(flawed), 1) if flawed else 0.0,
            "false_fail_pct": round(100 * sum(not r["passed"] for r in good) / len(good), 1) if good else 0.0,
            "agreement_pct": round(100 * sum(len(v) == 1 for v in by_item.values()) / len(by_item), 1)
            if by_item else 0.0,
            "median_s": statistics.median(r["seconds"] for r in ok) if ok else 0.0,
            "errors": float(len(rows) - len(ok))}


def switch(candidate: dict[str, float], current: dict[str, float]) -> bool:
    """Brief E5 §4: switch only if catch +10 points, false fails not higher, agreement not lower, time <= 1.5x."""
    return (candidate["catch_pct"] >= current["catch_pct"] + 10
            and candidate["false_fail_pct"] <= current["false_fail_pct"]
            and candidate["agreement_pct"] >= current["agreement_pct"]
            and candidate["median_s"] <= 1.5 * current["median_s"])


def report(folder: Path) -> str:
    lines = ["| Judge | Catch % (20 flawed) | False-fail % (10 good) | Agreement % | Median s | Errors |",
             "|---|---|---|---|---|---|"]
    for f in sorted(folder.glob("judge-*.json")):
        data = json.loads(f.read_text())
        m = metrics(data["rows"])
        lines.append(f"| {data['judge']} | {m['catch_pct']} | {m['false_fail_pct']} | {m['agreement_pct']} | "
                     f"{m['median_s']} | {int(m['errors'])} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--eval", type=Path, required=True)
    b.add_argument("--model", default="qwen3.8-27b-8bit")
    r = sub.add_parser("run")
    r.add_argument("--judge", required=True)
    r.add_argument("--repeats", type=int, default=3)
    sub.add_parser("report")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        asyncio.run(build(json.loads(args.eval.read_text()), args.model))
    elif args.cmd == "run":
        pass_score = int(yaml.safe_load(GOLDEN.read_text()).get("pass_score", 4))
        print(asyncio.run(run(args.judge, args.repeats, pass_score)))
    else:
        print(report(LawPaths.from_env().outputs / "judge"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
