"""scripts/bench_models.py: request shaping, metrics, the tool-call gate and the generated table."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_models", REPO / "scripts" / "bench_models.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


def test_padding_reaches_the_context_target(bench: ModuleType) -> None:
    text = bench.padding(4096, 700)
    assert abs(len(text) / bench.CHARS_PER_TOKEN - (4096 - 700)) < 50
    assert bench.padding(500, 700) == ""


def test_nonce_opens_every_prompt_so_the_prefix_cache_cannot_serve_it(bench: ModuleType) -> None:
    a, _ = bench.messages_for("code", 4096, None, nonce="aaa")
    b, _ = bench.messages_for("code", 4096, None, nonce="bbb")
    assert a["messages"][0]["content"].startswith("[run aaa]")
    assert a["messages"][0]["content"][:12] != b["messages"][0]["content"][:12]


def test_toolcall_workload_uses_the_recorded_turn(bench: ModuleType) -> None:
    recorded = json.loads((REPO / "trained" / "bench" / "toolcall-pantry.json").read_text())
    body, max_tokens = bench.messages_for("toolcall", 32768, recorded, nonce="n1")
    assert body["temperature"] == 0 and body["tools"] == recorded["tools"] and max_tokens == 400
    assert body["messages"][0]["content"].startswith("[run n1]")
    assert body["messages"][-1] == recorded["messages"][-1]
    with pytest.raises(ValueError):
        bench.messages_for("toolcall", 4096, None)


def test_run_metrics(bench: ModuleType) -> None:
    r = bench.Run("code", 4096, prompt_tokens=4000, completion_tokens=501, ttft_s=2.0, total_s=12.0)
    assert r.prefill_tps == 2000 and r.decode_tps == 50
    assert bench.Run("code", 4096).decode_tps == 0


def test_first_tool_call_parses_and_rejects_bad_json(bench: ModuleType) -> None:
    msg = {"tool_calls": [{"function": {"name": "read", "arguments": '{"filePath": "a.java"}'}}]}
    assert bench.first_tool_call(msg) == {"name": "read", "arguments": {"filePath": "a.java"}}
    assert bench.first_tool_call({"content": "hi"}) is None
    with pytest.raises(json.JSONDecodeError):
        bench.first_tool_call({"tool_calls": [{"function": {"name": "read", "arguments": "{bad"}}]})


def test_tool_call_gate(bench: ModuleType) -> None:
    call = {"name": "read", "arguments": {"filePath": "a"}}
    runs = [bench.Run("toolcall", 4096, tool_call=call), bench.Run("toolcall", 32768, tool_call=call)]
    assert bench.tool_calls_stable(runs, call)
    assert not bench.tool_calls_stable(runs, {"name": "bash", "arguments": {}})
    assert not bench.tool_calls_stable([bench.Run("toolcall", 4096)], None)


def test_summary_and_report_skip_failed_runs(bench: ModuleType, tmp_path: Path) -> None:
    ok = [bench.Run("code", 4096, 4000, 101, 1.0, 3.0 + i, peak_mem_gb=30.0 + i) for i in range(3)]
    bad = bench.Run("code", 4096, error="ReadTimeout")
    s = bench.summarize([*ok, bad])
    assert s["n"] == 3 and s["decode_tps"] == 33.3 and s["peak_mem_gb"] == 32.0  # 50, 33.3, 25 tok/s
    res = bench.Result("baseline", "m", "2026-11-23T10:00:00", "0.8.0", [*ok, bad])
    (tmp_path / "bench-1-baseline.json").write_text(json.dumps(
        {"label": res.label, "model": res.model, "started": res.started, "omlx_version": res.omlx_version,
         "runs": [r.__dict__ for r in res.runs]}))
    table = bench.render(bench.load_results(tmp_path))
    assert "| baseline | m | 0.8.0 | code | 4K | 33.3 |" in table and "| 3/4 |" in table
