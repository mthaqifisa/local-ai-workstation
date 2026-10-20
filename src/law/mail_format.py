"""Turn LAW's Markdown into styled email paragraphs.

Apple Mail cannot be scripted with an HTML body (`html content` "does nothing", per its dictionary), but a message's
body is rich text whose paragraphs can be styled; Mail then sends it as HTML with a plain-text part. So emails are
built as a list of (style, text) paragraphs: no Markdown symbols reach the reader.
"""

from __future__ import annotations

import re

Block = tuple[str, str]  # (style, text); styles: title, subtitle, h2, h3, p, li, note, meta, space

INLINE = [
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r"\1 (\2)"),  # [text](url) -> text (url)
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"__([^_]+)__"), r"\1"),
    (re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"), r"\1"),
    (re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"<br\s*/?>", re.IGNORECASE), " "),
]


def plain(text: str) -> str:
    """Inline Markdown removed: bold/italic markers, code ticks, links written out."""
    for pattern, repl in INLINE:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


def markdown_blocks(markdown: str) -> list[Block]:
    """Markdown as styled paragraphs: headings, paragraphs, list items, tables as readable lines."""
    blocks: list[Block] = []
    para: list[str] = []
    header: list[str] | None = None

    def flush() -> None:
        if para:
            blocks.append(("p", plain(" ".join(para))))
            para.clear()

    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        stripped = line.strip()
        if not stripped:
            flush()
            header = None
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            flush()
            blocks.append(("h2" if len(m.group(1)) <= 1 else "h3", plain(m.group(2))))
            continue
        if stripped.startswith("|"):
            flush()
            cells = [plain(c) for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c):
                continue  # the separator row
            if header is None:
                header = cells
                continue
            pairs = [f"{h}: {c}" if h else c for h, c in zip(header, cells, strict=False) if c]
            blocks.append(("li", " · ".join(pairs) if len(header) > 1 else cells[0]))
            continue
        if stripped.startswith(": "):  # a table caption
            flush()
            blocks.append(("note", plain(stripped[2:])))
            continue
        if m := re.match(r"^([-*+]|\d+[.)])\s+(.*)$", stripped):
            flush()
            marker = "•" if m.group(1) in "-*+" else m.group(1)
            blocks.append(("li", f"{marker}  {plain(m.group(2))}"))
            continue
        if stripped.startswith(">"):
            flush()
            blocks.append(("note", plain(stripped.lstrip("> "))))
            continue
        para.append(stripped)
    flush()
    return blocks


def to_lines(blocks: list[Block]) -> str:
    """The body file the mail script reads: one paragraph per line, `style<TAB>text`, a spacer between sections."""
    out: list[str] = []
    for i, (style, text) in enumerate(blocks):
        if style in {"h2", "h3"} and i:
            out.append("space\t")
        out.append(f"{style}\t{text.replace(chr(9), ' ').replace(chr(10), ' ')}")
    return "\n".join(out) + "\n"
