"""The book's deterministic checks, run by .law/check.sh after tools/build.sh. Exit 1 with every problem listed.

1. book.pdf exists, has pages and is A4; book.html is one self-contained file with a title.
2. Every image book.md references exists; every diagram source has its rendered PNG.
3. Every diagram fits a page and stays readable: at column width its text is at least MIN_FONT_PT, and its height
   fits the text area. A diagram that fails is split into parts, never shrunk.
4. Every footnote is a citation the reader can open (it has an http(s) link), and every footnote used is defined.
5. No code line is long enough to run off the page.
6. Every figure reference (@fig:<name>) names a figure, and no text is raw Typst or HTML, which shows in one format
   only (seen 2026-11-24: `@fig:x`{=typst} read "drawn in : the ..." in the HTML).
Usage: python3 tools/verify.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from prepare import FIG_REF, IMAGE, TEXT_W_PT, figure_numbers, natural_pt, parts, source_text

TEXT_H_PT = 841.9 - 2 * 68.0 - 70  # A4 minus margins, less room for the caption and header
MIN_FONT_PT = 7.0
CODE_MAX = 92  # characters that fit a code line at the template's code size
FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$", re.MULTILINE)
FOOTNOTE_REF = re.compile(r"\[\^([^\]]+)\](?!:)")
URL = re.compile(r"https?://\S+")
FENCE = re.compile(r"^(```|~~~)")


def check_outputs(root: Path) -> list[str]:
    problems = []
    pdf, html = root / "book.pdf", root / "book.html"
    if not pdf.is_file() or pdf.stat().st_size == 0:
        problems.append("book.pdf is missing: run bash tools/build.sh")
    else:
        info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
        if not re.search(r"^Pages:\s+[1-9]", info, re.MULTILINE):
            problems.append("book.pdf has no pages")
        if not re.search(r"^Page size:.*\(A4\)", info, re.MULTILINE):
            problems.append("book.pdf is not A4")
    if not html.is_file() or "<title>" not in html.read_text(errors="replace"):
        problems.append("book.html is missing or has no <title>")
    elif re.search(r"""<img[^>]+src="(?!data:)""", html.read_text(errors="replace")):
        problems.append("book.html links an image instead of embedding it (it must be one self-contained file)")
    return problems


def check_images(text: str, root: Path) -> list[str]:
    problems = []
    for m in IMAGE.finditer(text):
        path = root / m.group("path")
        if not path.is_file():
            problems.append(f"image {m.group('path')} does not exist")
            continue
        if not m.group("cap").strip():
            problems.append(f"image {m.group('path')} has no caption")
        nat = natural_pt(path) if m.group("path").startswith("diagrams/") else None
        if nat is None:
            continue
        w_pt, h_pt, label_pt = nat
        scale = min(1.0, TEXT_W_PT / w_pt)
        font = label_pt * scale
        if font < MIN_FONT_PT:
            problems.append(f"diagram {m.group('path')} is too wide: at page width its text is {font:.1f} pt "
                            f"(minimum {MIN_FONT_PT:.0f}). Split it into parts, or lay it out top-to-bottom")
        if h_pt * scale > TEXT_H_PT:
            problems.append(f"diagram {m.group('path')} is too tall for one page ({h_pt * scale:.0f} pt of "
                            f"{TEXT_H_PT:.0f}). Split it into parts")
    for src in [*root.glob("diagrams/*.mmd"), *root.glob("diagrams/*.dot")]:
        png = src.with_suffix(".png")
        if not png.is_file() or png.stat().st_mtime < src.stat().st_mtime:
            problems.append(f"{src.relative_to(root)} is not rendered: run bash tools/build.sh")
    return problems


def check_citations(text: str) -> list[str]:
    problems = []
    defined: dict[str, str] = {}
    for m in FOOTNOTE_DEF.finditer(text):
        if m.group(1) in defined:
            problems.append(f"footnote [^{m.group(1)}] is defined twice (labels are shared by every chapter: "
                            f"make it unique, e.g. [^ch3-{m.group(1)}])")
        defined[m.group(1)] = m.group(2)
        if not URL.search(m.group(2)):
            problems.append(f"footnote [^{m.group(1)}] has no link the reader can open: {m.group(2)[:80]}")
    body = FOOTNOTE_DEF.sub("", text)
    for ref in sorted(set(FOOTNOTE_REF.findall(body)) - set(defined)):
        problems.append(f"footnote [^{ref}] is used but never defined")
    return problems


RAW = re.compile(r"`[^`]*`\{=(?:typst|html|latex)\}|^```\{=(?:typst|html|latex)\}", re.MULTILINE)


def check_references(text: str) -> list[str]:
    problems = []
    known = figure_numbers(text)
    for label in sorted({m.group(1) for m in FIG_REF.finditer(text)} - set(known)):
        problems.append(f"@{label} refers to no figure: give the figure {{#{label}}} or fix the name")
    for m in RAW.finditer(text):
        if not FIG_REF.fullmatch(m.group(0)):
            problems.append(f"raw format-only markup {m.group(0)[:40]!r}: it shows in one format only; use Markdown")
    return problems


def check_code(text: str) -> list[str]:
    problems, inside = [], False
    for n, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line.strip()):
            inside = not inside
            continue
        if inside and len(line) > CODE_MAX:
            problems.append(f"line {n}: code line of {len(line)} characters would run off the page "
                            f"(at most {CODE_MAX}); wrap it")
    return problems


def main() -> int:
    root = Path.cwd()
    if not (root / "book.md").is_file():
        print("verify: FAIL\n- book.md does not exist", file=sys.stderr)
        return 1
    text = source_text(root)
    problems = check_outputs(root) + check_images(text, root) + check_citations(text) + check_references(text)
    for part in parts(root):
        if part.is_file():
            problems += [f"{part.relative_to(root)}: {p}" for p in check_code(part.read_text())]
    if problems:
        print("verify: FAIL\n" + "\n".join(f"- {p}" for p in problems), file=sys.stderr)
        return 1
    print("verify: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
