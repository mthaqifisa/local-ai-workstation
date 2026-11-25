#!/usr/bin/env python3
"""QA calibration (owner 2026-11-25: "QA can be replaced too, right? ... run quick check"): does the QA model find
known bugs in a small product by using it, without false reports, and how fast?

    qa_calibration.py run --model gemma-4-31b-8bit [--context 28672]
    qa_calibration.py report

The product is `tipsplit`, a command-line bill splitter with a written spec and PLANTED bugs. It is tested through
LAW's own QA path: the same QA_TASK prompt, OpenCode qa agent and environment, with only the model swapped. When the
session stops before its report, the same session is asked to finish it (tasks.REPORT_DUE). A bug counts as found
when a new finding in .law/BUGS.md names it (its keywords); any other finding is listed for a person to judge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
from editor_calibration import has_report, opencode  # noqa: E402 - sibling script

from law import bugs  # noqa: E402
from law.handlers.base import Runtime  # noqa: E402
from law.handlers.tasks import QA_TASK, REPORT_DUE, agent_env  # noqa: E402
from law.paths import LawPaths  # noqa: E402

TIMEOUT_S = 40 * 60
SPEC = """# tipsplit: acceptance criteria

`python3 tipsplit.py <bill> <tip-percent> <people>` prints what each person pays.

1. Each share is (bill + tip) / people, rounded to the nearest sen (2 decimals, half up): 100 15 3 prints
   `Each pays RM38.33`; 20 0 3 prints `Each pays RM6.67`; 0.05 0 2 prints `Each pays RM0.03`.
2. The bill and the tip are non-negative numbers; a negative one is refused with `error: bill and tip must not be
   negative` and exit code 2.
3. People is a whole number of at least 1; 0 or less is refused with `error: people must be at least 1` and exit
   code 2. It never crashes with a Python traceback.
4. `--currency USD` prints `$` instead of `RM`.
"""
README = """# tipsplit

Split a bill. Run: `python3 tipsplit.py 100 15 3` (bill, tip percent, people). Option: `--currency USD`.
Spec: docs/proposal/proposal.md.
"""
# Planted: rounding truncates (criterion 1); negatives are accepted (2); zero people crashes (3). --currency works.
PRODUCT = '''import argparse
import math


def main() -> int:
    ap = argparse.ArgumentParser(prog="tipsplit")
    ap.add_argument("bill", type=float)
    ap.add_argument("tip", type=float)
    ap.add_argument("people", type=int)
    ap.add_argument("--currency", default="MYR", choices=["MYR", "USD"])
    a = ap.parse_args()
    total = a.bill * (1 + a.tip / 100)
    share = math.floor(total / a.people * 100) / 100
    symbol = "$" if a.currency == "USD" else "RM"
    print(f"Each pays {symbol}{share:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
PLANTED = {
    "Q1-rounding": ("round", "6.67", "6.66", "0.03", "0.02", "truncat", "floor"),
    "Q2-negative": ("negative",),
    "Q3-zero-people": ("zero", "ZeroDivision", "traceback", "people must be at least 1", " 0 people", "0 people"),
}


def prepare(work: Path) -> None:
    if work.exists():
        shutil.rmtree(work)
    (work / "docs" / "proposal").mkdir(parents=True)
    (work / ".law").mkdir()
    (work / "docs" / "proposal" / "proposal.md").write_text(SPEC)
    (work / "README.md").write_text(README)
    (work / "tipsplit.py").write_text(PRODUCT)
    (work / ".law" / "PROMPT.md").write_text("Build tipsplit, a command-line bill splitter, to the spec.\n")
    (work / ".law" / "REQUESTS.md").write_text("# Requests\n\n(none)\n")
    bugs.path(work).write_text(bugs.HEADER)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-qm", "eval"],
                   cwd=work, check=True)


def override(model: str, context: int) -> dict[str, Any]:
    return {"agent": {"qa": {"model": f"law/{model}"}},
            "provider": {"law": {"models": {model: {"name": model, "limit": {"context": context, "output": 6144}}}}}}


async def run(model: str, context: int) -> Path:
    paths = LawPaths.from_env()
    rt = Runtime.load(paths)
    work = paths.cache / "tmp" / "qa-eval" / model
    prepare(work)
    prompt = QA_TASK.format(name="tipsplit", version=1, folder=work, spec_file="docs/proposal/proposal.md",
                            now=time.strftime("%Y-%m-%d %H:%M"))
    env = {**os.environ, **agent_env(rt, "qa"), "OPENCODE_CONFIG_CONTENT": json.dumps(override(model, context))}
    base = ["opencode", "run", "--agent", "qa", "-m", f"law/{model}", "--format", "json", "--dir", str(work)]
    started = time.monotonic()
    events, timed_out, _ = await opencode([*base, prompt], work, env, TIMEOUT_S)
    reported, continued = has_report(events), False
    sid = next((e["sessionID"] for e in events if e.get("sessionID")), None)
    if not reported and sid and not timed_out:
        more, timed_out, _ = await opencode([*base, "--session", sid, REPORT_DUE], work, env, TIMEOUT_S)
        events += more
        reported, continued = has_report(more), True
    seconds = round(time.monotonic() - started)
    found = bugs.load(work)
    hits = {k: [b.id for b in found if any(w.lower() in (b.title + " " + b.body).lower() for w in words)]
            for k, words in PLANTED.items()}
    matched = {i for v in hits.values() for i in v}
    result = {"model": model, "context": context, "seconds": seconds, "report": reported, "continued": continued,
              "timed_out": timed_out, "found": sum(1 for v in hits.values() if v), "planted": len(PLANTED),
              "by_bug": hits, "qa_pack": sorted(p.name for p in (work / "qa").glob("*")) if (work / "qa").is_dir()
              else [], "other_findings": [{"id": b.id, "title": b.title} for b in found if b.id not in matched],
              "findings": [{"id": b.id, "title": b.title, "body": b.body[:800]} for b in found]}
    out = paths.outputs / "qa-cal" / f"qa-{time.strftime('%Y%m%d-%H%M%S')}-{model}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    return out


def report(folder: Path) -> str:
    rows = ["| model | found | other findings | minutes | report | qa pack files |", "|---|---|---|---|---|---|"]
    for f in sorted(folder.glob("qa-*.json")):
        d = json.loads(f.read_text())
        rows.append(f"| {d['model']} | {d['found']}/{d['planted']} | {len(d['other_findings'])} | "
                    f"{round(d['seconds'] / 60, 1)} | {d['report']} | {len(d['qa_pack'])} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", required=True)
    r.add_argument("--context", type=int, default=28672)
    sub.add_parser("report")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        print(asyncio.run(run(args.model, args.context)))
    else:
        print(report(LawPaths.from_env().outputs / "qa-cal"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
