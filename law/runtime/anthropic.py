"""Anthropic Messages API ⇄ OpenAI chat format (pure functions, no I/O).

Lets Claude Code and other Anthropic-style clients use the local models through the
same queue:  ANTHROPIC_BASE_URL=http://<mac>:4000  ANTHROPIC_AUTH_TOKEN=local

Supported: system prompt (string or blocks), text, images (base64 / url), tools,
tool_choice, tool_use / tool_result round-trips, stop_sequences, temperature, top_p,
max_tokens, streaming (message_start … message_stop with text and tool-use deltas).
Ignored on purpose: thinking blocks sent back by the client (local models can't verify
them), metadata, cache_control, betas.
"""
from __future__ import annotations

import json
from typing import Any, Iterator


class AnthropicError(ValueError):
    pass


# ── request: Anthropic → OpenAI ─────────────────────────────────────────
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(parts)


def _image_part(b: dict) -> dict:
    src = b.get("source") or {}
    if src.get("type") == "base64":
        url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
    elif src.get("type") == "url":
        url = src.get("url", "")
    else:
        raise AnthropicError(f"unsupported image source type '{src.get('type')}'")
    return {"type": "image_url", "image_url": {"url": url}}


def _user(content: Any) -> list[dict]:
    """One Anthropic user turn → OpenAI messages (tool results become role=tool)."""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    tools, parts, has_image = [], [], False
    for b in content or []:
        t = b.get("type")
        if t == "tool_result":
            body = _text_of(b.get("content"))
            if b.get("is_error"):
                body = "ERROR: " + body
            tools.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": body})
        elif t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image":
            parts.append(_image_part(b)); has_image = True
        elif t in ("thinking", "redacted_thinking", "document"):
            continue
        else:
            raise AnthropicError(f"unsupported content block '{t}' in a user message")
    out = list(tools)
    if parts:
        out.append({"role": "user", "content": parts if has_image else
                    "\n".join(p["text"] for p in parts)})
    return out


def _assistant(content: Any) -> dict:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    text, calls = [], []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "tool_use":
            calls.append({"id": b.get("id", ""), "type": "function",
                          "function": {"name": b.get("name", ""),
                                       "arguments": json.dumps(b.get("input") or {})}})
    msg: dict = {"role": "assistant", "content": "\n".join(text)}
    if calls:
        msg["tool_calls"] = calls
    return msg


def to_openai(req: dict) -> tuple[list[dict], dict]:
    if not isinstance(req.get("messages"), list) or not req["messages"]:
        raise AnthropicError("'messages' must be a non-empty list")
    if not req.get("max_tokens"):
        raise AnthropicError("'max_tokens' is required")
    msgs: list[dict] = []
    system = _text_of(req.get("system")) if req.get("system") else ""
    if system:
        msgs.append({"role": "system", "content": system})
    for m in req["messages"]:
        role = m.get("role")
        if role == "user":
            msgs.extend(_user(m.get("content")))
        elif role == "assistant":
            msgs.append(_assistant(m.get("content")))
        else:
            raise AnthropicError(f"unsupported role '{role}'")
    params: dict = {"max_tokens": int(req["max_tokens"])}
    for a, o in (("temperature", "temperature"), ("top_p", "top_p")):
        if a in req:
            params[o] = req[a]
    if req.get("stop_sequences"):
        params["stop"] = req["stop_sequences"]
    if req.get("tools"):
        params["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in req["tools"] if t.get("name")]
    tc = req.get("tool_choice")
    if tc:
        kind = tc.get("type")
        params["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}.get(kind) or \
            ({"type": "function", "function": {"name": tc.get("name")}} if kind == "tool" else "auto")
    return msgs, params


def count_tokens(req: dict) -> int:
    """Rough estimate (~4 characters per token) — good enough for client-side budgeting."""
    try:
        msgs, params = to_openai({**req, "max_tokens": req.get("max_tokens") or 1})
    except AnthropicError:
        msgs, params = [], {}
    return max(1, len(json.dumps(msgs)) // 4 + len(json.dumps(params.get("tools", []))) // 4)


# ── response: OpenAI → Anthropic ────────────────────────────────────────
STOP = {"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn"}


def _tool_input(args: str) -> dict:
    try:
        v = json.loads(args or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {"_raw_arguments": args}


def from_openai(res: dict, model: str, msg_id: str) -> dict:
    m = res.get("message") or {}
    blocks: list[dict] = []
    if m.get("content"):
        blocks.append({"type": "text", "text": m["content"]})
    for i, tc in enumerate(m.get("tool_calls") or []):
        fn = tc.get("function") or {}
        blocks.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{msg_id}_{i}",
                       "name": fn.get("name", ""), "input": _tool_input(fn.get("arguments", ""))})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    u = res.get("usage") or {}
    return {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": blocks,
            "stop_reason": STOP.get(res.get("finish_reason") or "stop", "end_turn"), "stop_sequence": None,
            "usage": {"input_tokens": int(u.get("prompt_tokens") or 0),
                      "output_tokens": int(u.get("completion_tokens") or 0)}}


class StreamTranslator:
    """Feed OpenAI stream deltas in, get Anthropic SSE events (name, data) out."""

    def __init__(self, model: str, msg_id: str):
        self.model, self.msg_id = model, msg_id
        self.index = -1
        self.open: str | None = None          # "text" | "tool:<openai index>"
        self.tool_blocks: dict[int, int] = {}
        self.out_tokens = 0
        self.saw_tool = False

    def start(self) -> Iterator[tuple[str, dict]]:
        yield "message_start", {"type": "message_start", "message": {
            "id": self.msg_id, "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}

    def _close(self) -> Iterator[tuple[str, dict]]:
        if self.open is not None:
            yield "content_block_stop", {"type": "content_block_stop", "index": self.index}
            self.open = None

    def _open(self, kind: str, block: dict) -> Iterator[tuple[str, dict]]:
        yield from self._close()
        self.index += 1
        self.open = kind
        yield "content_block_start", {"type": "content_block_start", "index": self.index, "content_block": block}

    def delta(self, d: dict) -> Iterator[tuple[str, dict]]:
        if d.get("content"):
            self.out_tokens += 1
            if self.open != "text":
                yield from self._open("text", {"type": "text", "text": ""})
            yield "content_block_delta", {"type": "content_block_delta", "index": self.index,
                                          "delta": {"type": "text_delta", "text": d["content"]}}
        for tc in d.get("tool_calls") or []:
            self.saw_tool = True
            oi = tc.get("index", 0)
            fn = tc.get("function") or {}
            key = f"tool:{oi}"
            if oi not in self.tool_blocks:
                yield from self._open(key, {"type": "tool_use", "id": tc.get("id") or f"toolu_{self.msg_id}_{oi}",
                                            "name": fn.get("name") or "", "input": {}})
                self.tool_blocks[oi] = self.index
            elif self.open != key:
                continue                       # interleaved tool deltas are not expected; skip safely
            if fn.get("arguments"):
                self.out_tokens += 1
                yield "content_block_delta", {"type": "content_block_delta", "index": self.index,
                                              "delta": {"type": "input_json_delta",
                                                        "partial_json": fn["arguments"]}}

    def finish(self, finish_reason: str | None) -> Iterator[tuple[str, dict]]:
        if self.index < 0:
            yield from self._open("text", {"type": "text", "text": ""})
        yield from self._close()
        reason = STOP.get(finish_reason or "stop", "end_turn")
        if self.saw_tool and reason == "end_turn":
            reason = "tool_use"
        yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None},
                                "usage": {"output_tokens": self.out_tokens}}
        yield "message_stop", {"type": "message_stop"}


def resolve_model(name: str, personas: dict, models: dict, rules: dict, default: str) -> str:
    """Anthropic model name → persona or model id."""
    name = (name or "").strip()
    if name in personas or name in models:
        return name
    low = name.lower()
    for key, target in (rules or {}).items():
        if key != "*" and key.lower() in low:
            return target
    return (rules or {}).get("*", default)
