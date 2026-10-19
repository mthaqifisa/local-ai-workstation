"""Streaming completions rebuild the same choice shape (content, tool calls) and emit throttled thoughts."""

from __future__ import annotations

import json

import httpx
import pytest

from law import llm as llm_mod
from law.llm import LLM


def sse(*chunks: dict) -> bytes:  # type: ignore[type-arg]
    body = "".join(f"data: {json.dumps({'choices': [c]})}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


@pytest.fixture()
def transport(monkeypatch: pytest.MonkeyPatch):
    holder: dict[str, bytes] = {}
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=holder["body"], headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(llm_mod, "THOUGHT_EVERY_S", 0.0)
    return holder


async def test_stream_assembles_content_and_emits_thoughts(transport):
    transport["body"] = sse(
        {"delta": {"reasoning_content": "Compute the rate. "}},
        {"delta": {"reasoning_content": "Monthly r = 0.0035."}},
        {"delta": {"content": "1,956"}}, {"delta": {"content": ".07"}, "finish_reason": "stop"})
    thoughts: list[str] = []

    async def on_thought(t: str) -> None:
        thoughts.append(t)

    out = await LLM("http://x/v1").chat("m", [{"role": "user", "content": "q"}], on_thought=on_thought)
    assert out == "1,956.07"
    assert thoughts and thoughts[-1] == "Compute the rate."  # last sentence not yet followed by whitespace


async def test_stream_assembles_tool_calls_split_across_chunks(transport):
    transport["body"] = sse(
        {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_", "arguments": '{"co'}}]}},
        {"delta": {"tool_calls": [{"index": 0, "function": {"name": "python", "arguments": 'de": "print(1)"}'}}]}},
        {"delta": {}, "finish_reason": "tool_calls"})

    async def on_thought(t: str) -> None:
        return None

    choice = await LLM("http://x/v1").complete({"model": "m", "messages": []}, on_thought=on_thought)
    call = choice["message"]["tool_calls"][0]
    assert call["id"] == "c1" and call["function"]["name"] == "run_python"
    assert json.loads(call["function"]["arguments"]) == {"code": "print(1)"}
    assert choice["finish_reason"] == "tool_calls"
