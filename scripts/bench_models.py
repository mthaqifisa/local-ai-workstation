#!/usr/bin/env python3
"""Benchmark a model configuration on oMLX and write docs/BENCHMARKS.md from the raw results (never hand-edited).

    bench_models.py run --label baseline --model qwen3.8-27b-8bit [--contexts 4096,32768] [--repeats 3]
    bench_models.py report

Workloads: code (~500 tokens), prose (~600 tokens) and a recorded OpenCode tool-call turn, each with the prompt padded
to the target context. One warm-up, then `repeats` timed runs. Per run: TTFT, prefill tok/s, decode tok/s and the peak
model memory oMLX reports (/api/status). Raw JSON lands in ~/LAW/outputs/bench/. Protocol: ENHANCE brief §4.1, ADR-0025.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from law import secrets  # noqa: E402 - the repo's own package, importable once src/ is on the path
from law.paths import LawPaths  # noqa: E402
from law.privacy import scrub  # noqa: E402

CHARS_PER_TOKEN = 3.6  # measured on this repo's Python source with the Qwen tokenizer; padding only, not billing
CODE_PROMPT = ("Write a complete, typed Python module implementing a thread-safe LRU cache with per-entry TTL, "
               "an eviction callback and stats. Include docstrings. Code only.")
PROSE_PROMPT = ("Write a 600-word explanation, for a product manager, of why local LLM decode speed is bound by "
                "memory bandwidth and what speculative decoding changes. Plain prose, no lists.")
WORKLOADS = {"code": (CODE_PROMPT, 500), "prose": (PROSE_PROMPT, 600)}


@dataclass
class Run:
    workload: str
    context: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ttft_s: float = 0.0
    total_s: float = 0.0
    peak_mem_gb: float = 0.0
    tool_call: dict[str, Any] | None = None
    error: str | None = None

    @property
    def decode_tps(self) -> float:
        """0 for tool calls: oMLX sends a parsed tool call as one chunk at the end, so there's no decode window."""
        if self.workload == "toolcall":
            return 0.0
        gen = self.total_s - self.ttft_s
        return (self.completion_tokens - 1) / gen if gen > 0 and self.completion_tokens > 1 else 0.0

    @property
    def prefill_tps(self) -> float:
        return self.prompt_tokens / self.ttft_s if self.ttft_s > 0 else 0.0


@dataclass
class Result:
    label: str
    model: str
    started: str
    omlx_version: str
    runs: list[Run] = field(default_factory=list)


def padding(target_tokens: int, budget_tokens: int) -> str:
    """Real reference text (this repo's source) of about `target_tokens - budget_tokens` tokens."""
    want = max(0, int((target_tokens - budget_tokens) * CHARS_PER_TOKEN))
    out: list[str] = []
    size = 0
    files = sorted((REPO / "src" / "law").rglob("*.py"))
    while size < want and files:
        for f in files:
            text = f"# file: {f.relative_to(REPO)}\n{f.read_text()}\n"
            out.append(text)
            size += len(text)
            if size >= want:
                break
    return "".join(out)[:want]


def messages_for(workload: str, context: int, recorded: dict[str, Any] | None,
                 nonce: str = "") -> tuple[dict[str, Any], int]:
    """The chat request body (minus model) and max_tokens for one workload at one context size.

    `nonce` opens the prompt so oMLX's prefix cache can't serve a timed run from the warm-up (prefill stays real).
    """
    head = f"[run {nonce}]\n" if nonce else ""
    if workload == "toolcall":
        if recorded is None:
            raise ValueError("toolcall workload needs --toolcall <recorded request JSON>")
        msgs = list(recorded["messages"])
        msgs[0] = {**msgs[0], "content": head + str(msgs[0]["content"])}
        msgs.insert(1, {"role": "user", "content": "Reference material:\n" + padding(context, 2000)})
        return {"messages": msgs, "tools": recorded["tools"], "temperature": 0}, 400
    prompt, max_tokens = WORKLOADS[workload]
    ref = padding(context, max_tokens + 200)
    content = head + (f"Reference material (you may ignore it):\n{ref}\n\n{prompt}" if ref else prompt)
    return {"messages": [{"role": "user", "content": content}], "temperature": 0}, max_tokens


def first_tool_call(message: dict[str, Any]) -> dict[str, Any] | None:
    """{"name", "arguments"(parsed)} of the first tool call, or None. Raises on arguments that aren't JSON."""
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    fn = calls[0]["function"]
    return {"name": fn["name"], "arguments": json.loads(fn.get("arguments") or "{}")}


class Omlx:
    def __init__(self, base: str, key: str) -> None:
        self.http = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=1800)

    def status(self) -> dict[str, Any]:
        data: dict[str, Any] = self.http.get("/api/status").json()
        return data

    def one(self, model: str, workload: str, context: int, recorded: dict[str, Any] | None) -> Run:
        body, max_tokens = messages_for(workload, context, recorded, nonce=uuid.uuid4().hex[:12])
        body.update(model=model, max_tokens=max_tokens, stream=True, stream_options={"include_usage": True},
                    chat_template_kwargs={"enable_thinking": False})
        run, peak, stop = Run(workload, context), [0], threading.Event()

        def sample() -> None:
            while not stop.is_set():
                with contextlib.suppress(httpx.HTTPError):
                    peak[0] = max(peak[0], int(self.status().get("model_memory_used", 0)))
                stop.wait(0.5)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        t0, first, text = time.monotonic(), 0.0, ""
        calls: list[dict[str, dict[str, str]]] = []
        try:
            with self.http.stream("POST", "/v1/chat/completions", json=body) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    chunk = json.loads(line[6:])
                    if chunk.get("usage"):
                        run.prompt_tokens = chunk["usage"].get("prompt_tokens", 0)
                        run.completion_tokens = chunk["usage"].get("completion_tokens", 0)
                    for ch in chunk.get("choices", []):
                        delta = ch.get("delta", {})
                        if not first and (delta.get("content") or delta.get("tool_calls")):
                            first = time.monotonic()
                        text += delta.get("content") or ""
                        for tc in delta.get("tool_calls") or []:
                            while len(calls) <= tc.get("index", 0):
                                calls.append({"function": {"name": "", "arguments": ""}})
                            fn = calls[tc.get("index", 0)]["function"]
                            fn["name"] += (tc.get("function") or {}).get("name") or ""
                            fn["arguments"] += (tc.get("function") or {}).get("arguments") or ""
            run.total_s = time.monotonic() - t0
            run.ttft_s = (first or time.monotonic()) - t0
            if workload == "toolcall":
                run.tool_call = first_tool_call({"content": text, "tool_calls": calls})
                if run.tool_call is None:
                    run.error = "no tool call"
        except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            stop.set()
            sampler.join(timeout=2)
        run.peak_mem_gb = round(peak[0] / 1e9, 2)
        return run


def summarize(runs: list[Run]) -> dict[str, Any]:
    ok = [r for r in runs if not r.error]
    if not ok:
        return {"n": 0}
    return {"n": len(ok), "decode_tps": round(statistics.median(r.decode_tps for r in ok), 1) or "-",
            "prefill_tps": round(statistics.median(r.prefill_tps for r in ok), 1),
            "ttft_s": round(statistics.median(r.ttft_s for r in ok), 2),
            "peak_mem_gb": max(r.peak_mem_gb for r in ok)}


def tool_calls_stable(runs: list[Run], baseline: dict[str, Any] | None) -> bool:
    """Every tool-call run parsed, and each matches the baseline's tool name and arguments (temperature 0)."""
    calls = [r.tool_call for r in runs if r.workload == "toolcall"]
    if not calls or any(c is None for c in calls):
        return False
    return baseline is None or all(c == baseline for c in calls)


def render(results: list[Result]) -> str:
    lines = ["# Model benchmarks", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `scripts/bench_models.py report` from raw results in "
             "`~/LAW/outputs/bench/`. Do not edit by hand. Medians of the timed runs; memory is the peak oMLX "
             "reported for loaded models. Tool-call rows have no decode figure (the call arrives as one chunk), "
             "so their TTFT is the whole turn; the Calls column says whether every run made the baseline's call. "
             "Protocol: ADR-0025.", "",
             "| Config | Model | oMLX | Workload | Context | Decode tok/s | Prefill tok/s | TTFT s | Peak GB | Runs "
             "| Calls |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    ref = next((r.tool_call for res in results for r in res.runs if r.tool_call), None)
    for res in results:
        groups: dict[tuple[str, int], list[Run]] = {}
        for r in res.runs:
            groups.setdefault((r.workload, r.context), []).append(r)
        for (wl, ctx), runs in sorted(groups.items()):
            s = summarize(runs)
            lines.append(f"| {res.label} | {res.model} | {res.omlx_version} | {wl} | {ctx // 1024}K | "
                         f"{s.get('decode_tps', '-')} | {s.get('prefill_tps', '-')} | {s.get('ttft_s', '-')} | "
                         f"{s.get('peak_mem_gb', '-')} | {int(s['n'])}/{len(runs)} | "
                         f"{('ok' if tool_calls_stable(runs, ref) else 'CHANGED') if wl == 'toolcall' else '-'} |")
    return scrub("\n".join(lines) + "\n")


def load_results(folder: Path) -> list[Result]:
    out = []
    for f in sorted(folder.glob("bench-*.json")):
        raw = json.loads(f.read_text())
        runs = [Run(**r) for r in raw.pop("runs")]
        out.append(Result(**raw, runs=runs))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--contexts", default="4096,32768")
    run.add_argument("--workloads", default="code,prose,toolcall")
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--toolcall", type=Path, help="recorded OpenCode request JSON (messages + tools)")
    sub.add_parser("report")
    args = ap.parse_args(argv)

    paths = LawPaths.from_env()
    out_dir = paths.outputs / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.cmd == "report":
        (REPO / "docs" / "BENCHMARKS.md").write_text(render(load_results(out_dir)))
        return 0

    key = secrets.read(paths.env_file).get("LAW_SERVICE_KEY", "")
    omlx = Omlx(os.environ.get("OMLX_URL", "http://127.0.0.1:8001"), key)
    recorded = json.loads(args.toolcall.read_text()) if args.toolcall else None
    res = Result(args.label, args.model, time.strftime("%Y-%m-%dT%H:%M:%S"), str(omlx.status().get("version")))
    workloads = [w for w in args.workloads.split(",") if w != "toolcall" or recorded]
    for ctx in (int(c) for c in args.contexts.split(",")):
        for wl in workloads:
            omlx.one(args.model, wl, ctx, recorded)  # warm-up: loads the model and fills the prefix cache
            for _ in range(args.repeats):
                r = omlx.one(args.model, wl, ctx, recorded)
                res.runs.append(r)
                print(f"{args.label} {wl} {ctx}: {r.decode_tps:.1f} tok/s decode, ttft {r.ttft_s:.2f}s"
                      + (f" ERROR {r.error}" if r.error else ""), flush=True)
    path = out_dir / f"bench-{time.strftime('%Y%m%d-%H%M%S')}-{args.label}.json"
    path.write_text(json.dumps({**{k: v for k, v in asdict(res).items() if k != "runs"},
                                "runs": [asdict(r) for r in res.runs]}, indent=2))
    print(path)
    return 0 if all(not r.error for r in res.runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
