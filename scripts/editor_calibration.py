#!/usr/bin/env python3
"""Editor calibration (owner 2026-11-25: "find the best model to do review of documents"): which model catches known
errors in a real chapter, without false alarms, and how fast?

    editor_calibration.py run --model gemma-4-31b-8bit [--project ~/RnD/digital-bank-ch1-trial] [--repeats 1]
    editor_calibration.py report                    # table from every run in ~/LAW/outputs/editor/

Each run copies the project's chapter, sources and notes into a scratch folder, plants PLANTS (each a known error
whose truth is in the saved sources), and fact-checks the planted sections through LAW's own editor path: the same
EDITOR_SECTION prompt, OpenCode editor agent, read limits and environment as a review round, with only the model
swapped (OPENCODE_CONFIG_CONTENT). A plant counts as caught when a new finding names it (its keywords). Every other
finding is listed for a person to judge: a real problem in the original text, or a false alarm.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from law import bugs  # noqa: E402 - the repo's own package, importable once src/ is on the path
from law.handlers.base import Runtime  # noqa: E402
from law.handlers.tasks import (  # noqa: E402
    EDITOR_DENY_READ,
    EDITOR_SECTION,
    REPORT_DUE,
    agent_env,
    doc_sections,
    json_report,
    read_limits,
)
from law.paths import LawPaths  # noqa: E402

CHAPTER = "chapters/01-what-a-digital-bank-is.md"
SECTIONS = ("How it differs from an e-money wallet", "Capital and headcount")
# everything a review round has, so no finding is about the copy itself (a first run reported the missing build)
COPY = ("chapters", "sources", "diagrams", "tools", "book.md", ".law/RESEARCH.md", ".law/STYLE.md", ".law/PROMPT.md",
        ".law/REQUESTS.md", ".law/check.sh", ".law/env.sh")
SECTION_TIMEOUT_S = 45 * 60


@dataclass(frozen=True)
class Plant:
    id: str
    find: str
    replace: str
    keywords: tuple[str, ...]  # a finding that names any of these (case-insensitive) caught it
    truth: str


PLANTS = (
    Plant("E1-msba-year", "The Money Services Business Act 2011 and", "The Money Services Business Act 2013 and",
          ("Money Services Business Act 2013", "MSBA 2013", "2011"), "MSBA is 2011 (sources/rmit-sep26.txt:151)"),
    Plant("E2-wrong-act", "The Financial Services Act 2013 or the Islamic Financial Services Act 2013[^ch1-dbf-def]",
          "The Financial Services Act 2013 or the Islamic Banking Act 1983[^ch1-dbf-def]",
          ("Islamic Banking Act", "1983"), "an Islamic digital bank is licensed under the IFSA 2013 (framework)"),
    Plant("E3-invented-cap",
          "behind deposits at licensed banks.[^ch1-dbf-def][^ch1-bnm-faq]",
          "behind deposits at licensed banks.[^ch1-dbf-def][^ch1-bnm-faq] BNM caps a digital bank's deposits at "
          "RM50,000 per customer during its first year of operation.[^ch1-bnm-faq]",
          ("RM50,000", "50,000"), "no source sets a per-customer deposit cap"),
    Plant("E4-capital", "and RM300 million by the end of the fifth year from",
          "and RM500 million by the end of the fifth year from",
          ("RM500", "500 million"), "RM300 million (sources/framework-2020.txt:879)"),
    Plant("E5-asset-threshold", "raised\nthe asset threshold to RM3 billion",
          "raised\nthe asset threshold to RM5 billion",
          ("RM5 billion", "5 billion"), "RM3 billion (sources/framework-2020.txt:698)"),
    Plant("E6-tcr", "Minimum Total Capital Ratio of 8%", "Minimum Total Capital Ratio of 10%",
          ("10%", "Total Capital Ratio", "TCR"), "8% (sources/framework-2020.txt:723)"),
    Plant("E7-invented-headcount",
          "No number. That is the finding,",
          "BNM also requires a digital bank to employ at least 150 staff by the end of its second year.[^ch1-dbf-plan] "
          "No number. That is the finding,",
          ("150",), "the framework sets no minimum headcount"),
)


def prepare(project: Path, work: Path) -> list[dict[str, str]]:
    """A scratch copy of the project with the plants applied; returns the sections to check."""
    if work.exists():
        shutil.rmtree(work)
    for rel in COPY:
        src, dst = project / rel, work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        elif src.is_file():
            shutil.copy2(src, dst)
    chapter = work / CHAPTER
    text = chapter.read_text()
    for p in PLANTS:
        if text.count(p.find) != 1:
            raise SystemExit(f"plant {p.id}: {p.find!r} occurs {text.count(p.find)} times in {CHAPTER}, not once")
        text = text.replace(p.find, p.replace)
    chapter.write_text(text)
    bugs.path(work).write_text(bugs.HEADER)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.name=eval", "-c", "user.email=eval@example.com", "commit", "-qm", "eval"],
                   cwd=work, check=True)
    return [s for s in doc_sections(work) if s["heading"] in SECTIONS]


def model_override(model: str, context: int) -> dict[str, Any]:
    """OpenCode config over the editor's: the candidate model in LAW's provider, and the editor's read limits."""
    limits: dict[str, Any] = json.loads(read_limits("editor", EDITOR_DENY_READ)["OPENCODE_CONFIG_CONTENT"])
    limits["agent"]["editor"]["model"] = f"law/{model}"
    limits["provider"] = {"law": {"models": {model: {"name": model,
                                                      "limit": {"context": context, "output": 6144}}}}}
    return limits


async def check_section(rt: Runtime, work: Path, model: str, context: int, sec: dict[str, str], n: int,
                        total: int) -> dict[str, Any]:
    fill = {"name": "Digital Bank chapter 1 (calibration copy)", "version": 1, "folder": work,
            "now": time.strftime("%Y-%m-%d %H:%M")}
    prompt = EDITOR_SECTION.format(**fill, n=n, total=total, **sec)
    env = {**os.environ, **agent_env(rt, "editor"),
           "OPENCODE_CONFIG_CONTENT": json.dumps(model_override(model, context))}
    before = {b.id for b in bugs.load(work)}
    started = time.monotonic()
    base = ["opencode", "run", "--agent", "editor", "-m", f"law/{model}", "--format", "json", "--dir", str(work)]
    events, timed_out, continued = await opencode([*base, prompt], work, env, SECTION_TIMEOUT_S)
    reported = has_report(events)
    sid = next((e["sessionID"] for e in events if e.get("sessionID")), None)
    if not reported and sid and not timed_out:  # LAW's path: the same session finishes its report (tasks.REPORT_DUE)
        left = max(60.0, SECTION_TIMEOUT_S - (time.monotonic() - started))
        more, timed_out, _ = await opencode([*base, "--session", sid, REPORT_DUE], work, env, left)
        events += more
        reported, continued = has_report(more), True
    seconds = time.monotonic() - started
    tools = sum(1 for e in events if e.get("type") == "tool_use")
    found = [b for b in bugs.load(work) if b.id not in before]
    last = [e["part"]["text"] for e in events if e.get("type") == "text" and e.get("part", {}).get("text")]
    return {"section": sec["heading"], "seconds": round(seconds), "tool_calls": tools, "report": reported,
            "continued": continued, "timed_out": timed_out, "last_text": (last[-1] if last else "")[-2000:],
            "findings": [{"id": b.id, "title": b.title, "body": b.body[:1500]} for b in found]}


async def opencode(cmd: list[str], work: Path, env: dict[str, str],
                   limit_s: float) -> tuple[list[dict[str, Any]], bool, bool]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=work, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), limit_s)
        timed_out = False
    except TimeoutError:
        proc.kill()
        out, timed_out = b"", True
    events = [json.loads(line) for line in out.decode(errors="replace").splitlines() if line.startswith("{")]
    return events, timed_out, False


def has_report(events: list[dict[str, Any]]) -> bool:
    """LAW's own test: the session's final answer parses as a report with results (an earlier message that only
    quotes the format does not count)."""
    texts = [e["part"]["text"] for e in events if e.get("type") == "text" and e.get("part", {}).get("text")]
    return bool(texts) and bool(json_report(texts[-1]).get("tested"))


def score(sections: list[dict[str, Any]]) -> dict[str, Any]:
    findings = [f for s in sections for f in s["findings"]]
    caught, matched = {}, set()
    for p in PLANTS:
        hits = [f["id"] for f in findings
                if any(k.lower() in (f["title"] + " " + f["body"]).lower() for k in p.keywords)]
        caught[p.id] = hits
        matched.update(hits)
    return {"caught": sum(1 for v in caught.values() if v), "planted": len(PLANTS), "by_plant": caught,
            "other_findings": [f for f in findings if f["id"] not in matched],
            "minutes": round(sum(s["seconds"] for s in sections) / 60, 1),
            "tool_calls": sum(s["tool_calls"] for s in sections),
            "reports": sum(1 for s in sections if s["report"]), "sections": len(sections)}


async def run(model: str, project: Path, repeats: int, context: int) -> Path:
    paths = LawPaths.from_env()
    rt = Runtime.load(paths)
    results = []
    for r in range(repeats):
        work = paths.cache / "tmp" / "editor-eval" / f"{model}-r{r}"
        secs = prepare(project, work)
        if len(secs) != len(SECTIONS):
            raise SystemExit(f"expected sections {SECTIONS}, found {[s['heading'] for s in secs]}")
        done = []
        for n, sec in enumerate(secs, start=1):
            print(f"{model} r{r}: section {n}/{len(secs)} {sec['heading']}", flush=True)
            done.append(await check_section(rt, work, model, context, sec, n, len(secs)))
            print(f"  {done[-1]['seconds']} s, {done[-1]['tool_calls']} tool calls, "
                  f"{len(done[-1]['findings'])} findings", flush=True)
        results.append({"repeat": r, "sections": done, "score": score(done)})
    out = paths.outputs / "editor" / f"editor-{time.strftime('%Y%m%d-%H%M%S')}-{model}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": model, "context": context, "plants": [p.__dict__ for p in PLANTS],
                               "runs": results}, indent=2))
    return out


def report(folder: Path) -> str:
    rows = ["| model | run | caught | other findings | minutes | tool calls | reports |",
            "|---|---|---|---|---|---|---|"]  # caught = recorded in BUGS.md, the only place a finding counts
    for f in sorted(folder.glob("editor-*.json")):
        d = json.loads(f.read_text())
        for r in d["runs"]:
            s = r["score"]
            rows.append(f"| {d['model']} | {f.stem[7:22]} r{r['repeat']} | {s['caught']}/{s['planted']} | "
                        f"{len(s['other_findings'])} | {s['minutes']} | {s['tool_calls']} | "
                        f"{s['reports']}/{s['sections']} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", required=True)
    r.add_argument("--project", type=Path, default=Path.home() / "RnD" / "digital-bank-ch1-trial")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--context", type=int, default=28672, help="the same for every model: a fair comparison")
    sub.add_parser("report")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        print(asyncio.run(run(args.model, args.project.expanduser(), args.repeats, args.context)))
    else:
        print(report(LawPaths.from_env().outputs / "editor"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
