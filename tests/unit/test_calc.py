"""The calculator tool and the capped tool loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from law import calc
from law.llm import LLM, chat_with_tools


async def test_run_python_prints_and_reports_errors(tmp_path: Path):
    py = Path(sys.executable)
    assert await calc.run_python(py, tmp_path, "print(round(25000*(1+0.065/12)**84))") == "39356"
    err = await calc.run_python(py, tmp_path, "1/0")
    assert err.startswith("(exit 1)") and "ZeroDivisionError" in err
    assert await calc.run_python(py, tmp_path, "x = 1") == "(no output; use print())"


async def test_run_python_times_out(tmp_path: Path):
    out = await calc.run_python(Path(sys.executable), tmp_path, "while True: pass", timeout_s=1)
    assert out == "error: timed out after 1s"


class ScriptedLLM(LLM):
    def __init__(self, replies: list[dict]) -> None:  # type: ignore[type-arg]
        super().__init__("http://fake/v1")
        self.replies = replies
        self.bodies: list[dict] = []  # type: ignore[type-arg]

    async def complete(self, body):  # type: ignore[no-untyped-def,override]
        self.bodies.append(body)
        return self.replies.pop(0)


def _tool_call(code: str) -> dict:  # type: ignore[type-arg]
    return {"message": {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "run_python", "arguments": json.dumps({"code": code})}}]},
        "finish_reason": "tool_calls"}


async def test_tool_loop_feeds_results_back_and_returns_final_answer():
    llm = ScriptedLLM([_tool_call("print(6*7)"), {"message": {"content": "The answer is 42"}, "finish_reason": "stop"}])
    seen: list[str] = []

    async def run_tool(name, args):  # type: ignore[no-untyped-def]
        seen.append(args["code"])
        return "42"

    out = await chat_with_tools(llm, "m", [{"role": "user", "content": "6x7?"}], [calc.TOOL_SPEC], run_tool,
                                temperature=0.2, reasoning="medium")
    assert out == "The answer is 42" and seen == ["print(6*7)"]
    assert llm.bodies[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "42"}


async def test_tool_loop_is_capped():
    llm = ScriptedLLM([_tool_call("print(1)")] * 7 + [{"message": {"content": "final"}, "finish_reason": "stop"}])

    async def run_tool(name, args):  # type: ignore[no-untyped-def]
        return "1"

    out = await chat_with_tools(llm, "m", [{"role": "user", "content": "loop"}], [calc.TOOL_SPEC], run_tool,
                                temperature=0.2, reasoning=None, max_calls=6)
    assert out == "final"
    assert "Tool budget used up" in llm.bodies[-1]["messages"][-1]["content"]
