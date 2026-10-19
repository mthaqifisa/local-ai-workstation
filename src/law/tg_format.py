"""Convert the models' Markdown into Telegram HTML (the subset Telegram renders).

Bold, italics, inline code, code blocks, links and headings map directly. Telegram has no tables, so
Markdown tables become aligned monospace blocks.
"""

from __future__ import annotations

import html
import re

CODE_BLOCK = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)


MAX_PRE_WIDTH = 44  # characters that fit a phone screen in Telegram's monospace font


def _table(rows: list[str]) -> str:
    cells = [[re.sub(r"\*\*|__|`", "", c).strip() for c in r.strip().strip("|").split("|")]
             for r in rows if not SEPARATOR.match(r)]
    width = max(len(r) for r in cells)
    cells = [r + [""] * (width - len(r)) for r in cells]
    cols = [max(len(r[i]) for r in cells) for i in range(width)]
    if sum(cols) + 2 * (width - 1) > MAX_PRE_WIDTH and len(cells) > 1:
        return _records(cells)
    lines = ["  ".join(c.ljust(cols[i]) for i, c in enumerate(r)).rstrip() for r in cells]
    if len(lines) > 1:
        lines.insert(1, "  ".join("-" * w for w in cols))
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def _records(cells: list[list[str]]) -> str:
    """A wide table as one block per row: the first column as a bold title, other cells as labelled bullets."""
    header, body = cells[0], cells[1:]
    out = []
    for row in body:
        lines = [f"<b>{html.escape(row[0], quote=False)}</b>"]
        lines += [f"• {html.escape(header[i], quote=False)}: {html.escape(row[i], quote=False)}"
                  for i in range(1, len(row)) if row[i]]
        out.append("\n".join(lines))
    return "\n\n".join(out)


def to_telegram_html(markdown: str) -> str:
    blocks: list[str] = []

    def stash(match: re.Match[str]) -> str:
        blocks.append("<pre>" + html.escape(match.group(1).rstrip("\n"), quote=False) + "</pre>")
        return f"\x00{len(blocks) - 1}\x00"

    text = CODE_BLOCK.sub(stash, markdown)
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if TABLE_ROW.match(line):
            rows = []
            while i < len(lines) and (TABLE_ROW.match(lines[i]) or SEPARATOR.match(lines[i])):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        if line.lstrip().startswith(">"):
            quoted = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quoted.append(_inline(re.sub(r"^\s*>\s?", "", lines[i])))
                i += 1
            out.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>")
        elif re.match(r"^\s*([-*•])\s+", line):
            out.append("• " + _inline(re.sub(r"^\s*([-*•])\s+", "", line)))
        elif re.match(r"^\s*-{3,}\s*$", line):
            out.append("")
        else:
            out.append(_inline(line))
        i += 1
    result = "\n".join(out)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], result)
