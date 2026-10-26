"""Client for the local LLM endpoint (oMLX via Caddy): chat, embeddings, rerank, model control."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove reasoning blocks some models emit inline, keeping only the answer."""
    text = THINK_RE.sub("", text)
    if "</think>" in text:  # opening tag was in the prompt template
        text = text.split("</think>", 1)[1]
    return text.strip()


def image_part(path: Path) -> dict[str, Any]:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


Thought = Callable[[str], Awaitable[None]]
THOUGHT_EVERY_S = 3.0


def latest_thought(reasoning: str, limit: int = 220) -> str:
    """The most recent complete sentence or line of a model's reasoning, trimmed for a status message."""
    text = reasoning.strip()
    pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    if not pieces:
        return ""
    # A sentence is finished only once whitespace follows it: a stream can stop mid-number ("14." of "14.632").
    finished = reasoning.endswith(("\n", ". ", "! ", "? "))
    last = pieces[-1] if len(pieces) == 1 or finished else pieces[-2]
    return last if len(last) <= limit else "…" + last[-limit:]


class TruncatedError(RuntimeError):
    """The model spent its whole token allowance reasoning and produced no answer."""


class LLM:
    def __init__(self, base_url: str, admin_url: str | None = None, api_key: str = "local",
                 timeout_s: float = 3600) -> None:
        self.base = base_url.rstrip("/")
        self.admin = (admin_url or base_url.rsplit("/v1", 1)[0]).rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout = httpx.Timeout(timeout_s, connect=10)

    async def chat(self, model: str, messages: list[dict[str, Any]], *, temperature: float = 0.6,
                   max_tokens: int = 24576, reasoning: str | None = None,
                   response_format: dict[str, Any] | None = None, extra: dict[str, Any] | None = None,
                   on_thought: Thought | None = None) -> str:
        """One completion. Thinking and answer share `max_tokens`; an empty answer cut off by the limit is an
        error (retried by the queue at lower effort) rather than a silent empty result."""
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens, "stream": False, **(extra or {})}
        if reasoning == "off":  # the chat template's switch; reasoning_effort cannot turn thinking off
            body["chat_template_kwargs"] = {"enable_thinking": False}
        elif reasoning:
            body["reasoning_effort"] = reasoning
        if response_format:
            body["response_format"] = response_format
        choice = await (self.complete(body, on_thought=on_thought) if on_thought else self.complete(body))
        answer = strip_thinking(choice["message"].get("content") or "")
        if not answer and choice.get("finish_reason") == "length":
            raise TruncatedError(f"{model} used all {max_tokens} tokens thinking and gave no answer")
        return answer

    async def complete(self, body: dict[str, Any], on_thought: Thought | None = None) -> dict[str, Any]:
        """POST one chat completion and return its first choice (the single seam tests replace).

        With `on_thought`, the request streams and the latest line of reasoning is passed out every few
        seconds (live thinking); the returned choice has the same shape either way."""
        if on_thought is None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.base}/chat/completions", json=body, headers=self.headers)
                r.raise_for_status()
                choice: dict[str, Any] = r.json()["choices"][0]
                return choice
        return await self._complete_streaming(body, on_thought)

    async def _complete_streaming(self, body: dict[str, Any], on_thought: Thought) -> dict[str, Any]:
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish: str | None = None
        last_sent = 0.0
        async with httpx.AsyncClient(timeout=self.timeout) as client, client.stream(
                "POST", f"{self.base}/chat/completions", json={**body, "stream": True}, headers=self.headers) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                choice = json.loads(line[6:])["choices"][0]
                delta = choice.get("delta") or {}
                finish = choice.get("finish_reason") or finish
                if delta.get("content"):
                    content.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                                 "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
                now = time.monotonic()
                if reasoning and now - last_sent > THOUGHT_EVERY_S:
                    last_sent = now
                    await on_thought(latest_thought("".join(reasoning)))
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {"message": message, "finish_reason": finish}

    async def stream(self, model: str, messages: list[dict[str, Any]], *, temperature: float = 0.6,
                     max_tokens: int = 8192) -> AsyncIterator[str]:
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                "stream": True}
        async with httpx.AsyncClient(timeout=self.timeout) as client, client.stream(
                "POST", f"{self.base}/chat/completions", json=body, headers=self.headers) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                delta = json.loads(line[6:])["choices"][0].get("delta", {})
                if delta.get("content"):
                    yield delta["content"]

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base}/embeddings", json={"model": model, "input": texts},
                                  headers=self.headers)
            r.raise_for_status()
            return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]

    async def rerank(self, model: str, query: str, documents: list[str]) -> list[tuple[int, float]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base}/rerank", json={"model": model, "query": query,
                                                                "documents": documents}, headers=self.headers)
            r.raise_for_status()
            return [(d["index"], float(d["relevance_score"])) for d in r.json()["results"]]

    async def models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            r = await client.get(f"{self.base}/models", headers=self.headers)
            r.raise_for_status()
            return [m["id"] for m in r.json()["data"]]


class OmlxModelControl:
    """Frees unified memory before a heavy job by unloading resident heavy LLMs through oMLX.

    Light models (embeddings, reranker) stay loaded. LLM-to-LLM swaps unload the old model first so
    oMLX never has to evict mid-request; media jobs unload every heavy LLM.
    """

    def __init__(self, internal_base: str, light_ids: set[str]) -> None:
        self.base = internal_base.rstrip("/")
        self.light = light_ids

    async def loaded(self) -> list[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
            r = await client.get(f"{self.base}/v1/models/status")
            r.raise_for_status()
            return [m["id"] for m in r.json()["models"] if m.get("loaded")]

    async def unload(self, model_id: str) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
            r = await client.post(f"{self.base}/v1/models/{model_id}/unload")
            r.raise_for_status()

    async def prepare(self, resource: str, resident: str | None) -> None:
        keep = resource.removeprefix("llm:") if resource.startswith("llm:") else None
        for model_id in await self.loaded():
            if model_id in self.light or model_id == keep:
                continue
            await self.unload(model_id)


ToolRunner = Callable[[str, dict[str, Any]], Awaitable[str]]


async def chat_with_tools(llm: LLM, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                          run_tool: ToolRunner, *, temperature: float, reasoning: str | None,
                          max_calls: int = 6, on_thought: Thought | None = None,
                          extra: dict[str, Any] | None = None) -> str:
    """Chat loop that executes the model's tool calls (capped) and returns its final text answer."""
    history = list(messages)
    for _ in range(max_calls + 1):
        body: dict[str, Any] = {"model": model, "messages": history, "temperature": temperature,
                                "max_tokens": 24576, "stream": False, "tools": tools, **(extra or {})}
        if reasoning:
            body["reasoning_effort"] = reasoning
        choice = await (llm.complete(body, on_thought=on_thought) if on_thought else llm.complete(body))
        msg = choice["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            text = strip_thinking(msg.get("content") or "")
            if not text and choice.get("finish_reason") == "length":
                raise TruncatedError(f"{model} used all tokens thinking and gave no answer")
            return text
        history.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if on_thought:
                    await on_thought(f"running {fn['name']}…")
                result = await run_tool(fn["name"], args)
            except Exception as exc:  # the model sees the error and can correct itself
                result = f"error: {type(exc).__name__}: {exc}"
            history.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result[:6000]})
    history.append({"role": "user", "content": "Tool budget used up. Give your final answer now from what you have."})
    return await llm.chat(model, history, temperature=temperature, reasoning=reasoning, on_thought=on_thought,
                          extra=extra)
