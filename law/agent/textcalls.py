"""Rescue tool calls that the model wrote as TEXT instead of real tool calls.

Local models served by mlx-openai-server don't always come back with clean `tool_calls`:
  - Qwen3.x chat templates open the thinking block in the prompt, so the reply can start
    INSIDE <think> and the reasoning parser can miss it — the thinking ("Simple task…
    </think>") lands in `content`.
  - the tool-call parser can miss a call, leaving it in `content` as text, in one of these
    formats:
        <tool_call>{"name": "write_file", "arguments": {...}}</tool_call>       (Qwen / Hermes)
        <tool_call><function=write_file><parameter=path>a.py</parameter>…</function></tool_call>
                                                                               (Qwen3-Coder XML)
        ```json {"name": "write_file", "arguments": {...}} ```                  (plain JSON)

split_think() separates thinking from the answer; parse_text_calls() turns any of the
formats above into normal OpenAI tool calls (only for known tool names), and strip_calls()
removes them from the text.
"""
from __future__ import annotations

import json
import re

TOOL_CALL_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
FUNCTION_XML = re.compile(r"<function=([\w.\-]+)>(.*?)(?:</function>|(?=<function=)|$)", re.S)
PARAM_XML = re.compile(r"<parameter=([\w.\-]+)>\n?(.*?)\n?</parameter>", re.S)
FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def split_think(content: str, reasoning: str = "") -> tuple[str, str]:
    """→ (answer, reasoning). Moves '<think>…</think>' (or an unopened '…</think>') out of content."""
    content = content or ""
    reasoning = reasoning or ""
    if "</think>" in content:
        before, after = content.rsplit("</think>", 1)
        before = before.replace("<think>", "").strip()
        reasoning = (reasoning + "\n" + before).strip() if before else reasoning
        content = after
    content = re.sub(r"(?s)<think>.*?</think>", "", content)
    return content.strip(), reasoning.strip()


def _call(name: str, args) -> dict:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            pass
    return {"type": "function", "function": {"name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args if args is not None else {})}}


def _from_json_obj(obj, names: set[str]) -> dict | None:
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):            # {"function": {"name":..,"arguments":..}}
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    if not isinstance(name, str) or (names and name not in names):
        return None
    args = obj.get("arguments", obj.get("parameters", obj.get("args", obj.get("input", {}))))
    return _call(name, args)


def _xml_value(v: str):
    s = v.strip()
    if s[:1] in "[{" or s in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", s):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    return v


def _from_xml(body: str, names: set[str]) -> list[dict]:
    out = []
    for name, inner in FUNCTION_XML.findall(body):
        if names and name not in names:
            continue
        out.append(_call(name, {k: _xml_value(v) for k, v in PARAM_XML.findall(inner)}))
    return out


def _json_objects(text: str) -> list:
    """Every top-level {...} in text that parses as JSON."""
    objs, dec, i = [], json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
            objs.append(obj)
            i = end
        except json.JSONDecodeError:
            i += 1
    return objs


def parse_text_calls(text: str, names) -> list[dict]:
    names = set(names or [])
    text = text or ""
    calls: list[dict] = []
    for block in TOOL_CALL_BLOCK.findall(text):
        if "<function=" in block:
            calls += _from_xml(block, names)
        else:
            for obj in _json_objects(block):
                c = _from_json_obj(obj, names)
                if c:
                    calls.append(c)
    if not calls and "<function=" in text:
        calls = _from_xml(text, names)
    if not calls:
        for blob in FENCED_JSON.findall(text):
            for obj in _json_objects(blob):
                c = _from_json_obj(obj, names)
                if c:
                    calls.append(c)
    if not calls and names:
        stripped = text.strip()
        if stripped.startswith("{"):
            for obj in _json_objects(stripped):
                c = _from_json_obj(obj, names)
                if c:
                    calls.append(c)
    return calls


def strip_calls(text: str) -> str:
    text = TOOL_CALL_BLOCK.sub("", text or "")
    text = FUNCTION_XML.sub("", text)
    text = FENCED_JSON.sub("", text)
    return text.strip()
