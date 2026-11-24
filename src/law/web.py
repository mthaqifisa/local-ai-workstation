"""Web search and page reading for every agent, by default (owner 2026-11-24: "all agents should have web search
capability ... it should be a default", so that data is current and accurate).

OpenCode agents search with `law search` in their shell and read pages with webfetch; a role answered by a direct
model call (the planner, the chat assistant) gets the same two abilities as the tools below. Search runs on LAW's own
SearXNG; nothing leaves the Mac but the query and the page request.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import httpx

PAGE_CHARS = 12000  # of a page's text into the prompt: enough for the passage, not a whole site
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36"
DROP = re.compile(r"(?is)<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>")
TAG = re.compile(r"(?s)<[^>]+>")
BLOCK = re.compile(r"(?i)</?(p|div|li|h[1-6]|tr|br|section|article|table)\b[^>]*>")

SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": ("Search the web (LAW's own SearXNG). Use it whenever a fact may have changed or you are not "
                        "sure of it: figures, rules, versions, dates, names. Returns title, url and snippet for each "
                        "result; open the best official one with fetch_page to confirm."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
}
FETCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "fetch_page",
        "description": (f"Read a web page or PDF as plain text (the first {PAGE_CHARS} characters, or the passages "
                        "around `find` when given). Use it to confirm what a search result says."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "find": {"type": "string", "description": "Optional words to look for; returns the passages around them"},
        }, "required": ["url"]},
    },
}
TOOLS = [SEARCH_TOOL, FETCH_TOOL]


async def search(port: int, query: str, limit: int = 8) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"http://127.0.0.1:{port}/search", params={"q": query, "format": "json"})
        r.raise_for_status()
        results = r.json().get("results", [])[:limit]
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in results]


def html_text(page: str) -> str:
    text = BLOCK.sub("\n", DROP.sub(" ", page))
    text = html.unescape(TAG.sub(" ", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def passages(text: str, find: str, width: int = 600) -> str:
    """The text around every place one of `find`'s words occurs, overlapping passages merged, in page order."""
    words = [w for w in re.findall(r"\w+", find.lower()) if len(w) > 2]
    if not words:
        return text[:PAGE_CHARS]
    low = text.lower()
    spans: list[tuple[int, int]] = []
    for w in words:
        for m in re.finditer(re.escape(w), low):
            spans.append((max(0, m.start() - width), min(len(text), m.end() + width)))
    if not spans:
        return f"(none of {find!r} on this page)\n" + text[:2000]
    spans.sort()
    merged = [spans[0]]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return "\n…\n".join(text[a:b] for a, b in merged)[:PAGE_CHARS]


async def pdf_text(data: bytes) -> str:
    proc = await asyncio.create_subprocess_exec("pdftotext", "-layout", "-", "-", stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate(data)
    return out.decode(errors="replace")


async def fetch(url: str, find: str = "") -> str:
    if not re.match(r"https?://", url):
        return "error: give a full http(s) URL"
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(url)
    except httpx.HTTPError as exc:
        return f"error: could not open {url}: {exc}"
    if r.status_code >= 400:
        return f"error: {url} answered HTTP {r.status_code}"
    kind = r.headers.get("content-type", "")
    if "pdf" in kind or r.content[:5] == b"%PDF-":
        text = await pdf_text(r.content)
    elif "html" in kind or "xml" in kind or not kind:
        text = html_text(r.text)
    else:
        text = r.text
    body = passages(text, find) if find else text[:PAGE_CHARS]
    more = "" if len(text) <= PAGE_CHARS or find else f"\n… ({len(text) - PAGE_CHARS} more characters: use find)"
    return f"{r.url}\n\n{body}{more}"
