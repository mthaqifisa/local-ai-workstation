"""The book's deterministic checks, run by .law/check.sh after tools/build.sh. Exit 1 with every problem listed.

1. book.pdf exists, has pages and is A4; book.html is one self-contained file with a title.
2. Every image book.md references exists; every diagram source has its rendered PNG.
3. Every diagram fits a page and stays readable: at column width its text is at least MIN_FONT_PT, and its height
   fits the text area. A diagram that fails is split into parts, never shrunk.
4. Every footnote is a citation the reader can open (it has an http(s) link), and every footnote used is defined.
5. No code line is long enough to run off the page.
6. Every figure reference (@fig:<name>) names a figure, and no text is raw Typst or HTML, which shows in one format
   only (seen 2026-11-24: `@fig:x`{=typst} read "drawn in : the ..." in the HTML).
7. Readable by every reader the document is for (.law/STYLE.md names them): no sentence over SENTENCE_MAX words, a section's sentences average at most SENTENCE_AVG,
   dashes stay rare, a quotation over QUOTE_MAX words is paraphrased and cited instead, and a sequence diagram has
   no self-message (its label sits on the lifeline; use a Note) and no label over LABEL_MAX characters.
   The limits suit most books and reports; a document sets its own in book.md's front matter (`sentence-max`,
   `sentence-average`, `dashes-per-1000`, `quote-max`), e.g. a research paper with longer sentences.
   The gate checks the text and diagrams this change adds or edits (`git diff HEAD`), so a ticket answers for its
   own writing; `--all` checks the whole book (the editor's view).
Usage: python3 tools/verify.py [--all]
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
SENTENCE_MAX = 40  # words; measured 2026-11-26: the tech trial's prose averaged 40 and lost its non-expert readers
SENTENCE_AVG = 22
DASHES_PER_1000 = 8
QUOTE_MAX = 30
LABEL_MAX = 60
NOT_PROSE = re.compile(r"^\s*(\||#|!\[|\[\^|>|<|---|:::|\{)")
ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|cf|Dr|Mr|Ms|No|Fig|Sec|Art|para)\.", re.IGNORECASE)
SENT_SPLIT = re.compile(r"(?<=[.!?])[\"”’)]*\s+(?=[A-Z0-9“\"(])")
QUOTED = re.compile(r"“([^”]*)”|\"([^\"]*)\"")  # left to right, so one quote's end never pairs with the next
SELF_MSG = re.compile(r"^\s*(\w+)\s*-[-)x>]*>>?[+-]?\s*\1\s*:", re.MULTILINE)
SEQ_LABEL = re.compile(r"^\s*\w+\s*-[-)x>]+[+-]?\s*\w+\s*:\s*(.+)$", re.MULTILINE)


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


def changed_lines(root: Path, path: Path) -> set[int] | None:
    """Line numbers this change adds or edits in `path` (None: all of it, e.g. a new file or no git)."""
    rel = str(path.relative_to(root))
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=root, capture_output=True)
    if tracked.returncode != 0:
        return None
    diff = subprocess.run(["git", "diff", "-U0", "HEAD", "--", rel], cwd=root, capture_output=True, text=True)
    lines: set[int] = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff.stdout, re.MULTILINE):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        lines.update(range(start, start + count))
    return lines


def paragraphs(text: str) -> list[tuple[int, int, str]]:
    """Prose paragraphs as (first line, last line, text): no code, tables, headings, figures, quotes or notes."""
    out, inside, block, first = [], False, [], 0
    for n, line in enumerate(text.splitlines() + [""], start=1):
        if FENCE.match(line.strip()):
            inside = not inside
            line = ""
        if inside or not line.strip() or NOT_PROSE.match(line):
            if block:
                out.append((first, n - 1, " ".join(block)))
            block = []
            continue
        if block and re.match(r"^\s*(?:[-*+]|\d+\.)\s+", line):  # a list item is a paragraph of its own
            out.append((first, n - 1, " ".join(block)))
            block = []
        if not block:
            first = n
        block.append(re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", line.strip()))
    return out


def sentences(prose: str) -> list[str]:
    clean = re.sub(r"\[\^[^\]]+\]", "", prose)
    clean = re.sub(r"`[^`]*`", "X", clean)
    clean = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", clean)
    clean = ABBREV.sub(lambda m: m.group(0).replace(".", ""), clean)
    return [x for x in SENT_SPLIT.split(clean) if len(x.split()) >= 3]


def limits(root: Path) -> dict[str, int]:
    """Rule 7's limits: the defaults, or the document's own from book.md's front matter."""
    values = {"sentence-max": SENTENCE_MAX, "sentence-average": SENTENCE_AVG,
              "dashes-per-1000": DASHES_PER_1000, "quote-max": QUOTE_MAX}
    try:
        head = (root / "book.md").read_text().split("\n---", 1)[0]
    except OSError:
        return values
    for key in values:
        found = re.search(rf"^{key}:\s*(\d+)\s*$", head, re.MULTILINE)
        if found:
            values[key] = int(found.group(1))
    return values


def check_style(root: Path, part: Path, whole: bool) -> list[str]:
    """Rule 7 for one chapter file: the paragraphs this change touched, or all of them with --all."""
    text = part.read_text()
    lim = limits(root)
    longest, average, dash_rate, quote_max = (lim["sentence-max"], lim["sentence-average"], lim["dashes-per-1000"],
                                              lim["quote-max"])
    changed = None if whole else changed_lines(root, part)
    if changed is not None and not changed:
        return []
    paras = [p for p in paragraphs(text) if changed is None or any(p[0] <= n <= p[1] for n in changed)]
    problems, lengths, words, dashes = [], [], 0, 0
    heads = [(n, line.lstrip("#").strip()) for n, line in enumerate(text.splitlines(), start=1)
             if re.match(r"^#{1,2} ", line)]
    long_by_section: dict[str, list[int]] = {}
    for first, _, prose in paras:
        for sent in sentences(prose):
            size = len(sent.split())
            lengths.append(size)
            if size > longest and whole:  # the editor's view: one line per section
                section = next((h for n, h in reversed(heads) if n <= first), "(before the first heading)")
                long_by_section.setdefault(section, []).append(first)
            elif size > longest:
                problems.append(f"line {first}: a {size}-word sentence (at most {longest}; split it): "
                                f"\"{' '.join(sent.split()[:12])} ...\"")
        words += len(prose.split())
        dashes += prose.count("—") + prose.count(" – ")
        for q in QUOTED.finditer(prose):
            quote = q.group(1) or q.group(2) or ""
            if len(quote.split()) > quote_max:
                problems.append(f"line {first}: a {len(quote.split())}-word quotation (at most {quote_max}): "
                                "quote the key phrase and paraphrase the rest, with its footnote")
    for section, lines in long_by_section.items():
        shown = ", ".join(str(n) for n in sorted(set(lines))[:12]) + (" ..." if len(set(lines)) > 12 else "")
        problems.append(f'"{section}": {len(lines)} sentences over {longest} words (paragraphs at lines {shown})')
    if len(lengths) >= 10 and sum(lengths) / len(lengths) > average:
        problems.append(f"sentences average {sum(lengths) / len(lengths):.0f} words (at most {average}): "
                        "one idea per sentence")
    if words >= 300 and dashes * 1000 / words > dash_rate:
        problems.append(f"{dashes} dashes in {words} words ({dashes * 1000 / words:.0f} per 1,000; at most "
                        f"{dash_rate}): use full stops, commas or brackets")
    return problems


def check_diagrams(root: Path, whole: bool) -> list[str]:
    problems = []
    for src in sorted((root / "diagrams").glob("*.mmd")):
        if not whole and changed_lines(root, src) == set():
            continue
        text = src.read_text()
        name = src.relative_to(root)
        if not re.match(r"\s*(%%.*\n\s*)*sequenceDiagram\b", text):  # a state's self-transition is fine
            continue
        for m in SELF_MSG.finditer(text):
            problems.append(f"{name}: '{m.group(0).strip()[:50]}' is a self-message, whose label is drawn over the "
                            "lifeline: write it as `Note right of <who>: ...`")
        for m in SEQ_LABEL.finditer(text):
            if len(m.group(1)) > LABEL_MAX:
                problems.append(f"{name}: a {len(m.group(1))}-character label (at most {LABEL_MAX}): shorten it")
    return problems


def main() -> int:
    root = Path.cwd()
    if not (root / "book.md").is_file():
        print("verify: FAIL\n- book.md does not exist", file=sys.stderr)
        return 1
    text = source_text(root)
    problems = check_outputs(root) + check_images(text, root) + check_citations(text) + check_references(text)
    whole = "--all" in sys.argv[1:]
    for part in parts(root):
        if part.is_file():
            problems += [f"{part.relative_to(root)}: {p}" for p in check_code(part.read_text())]
            problems += [f"{part.relative_to(root)}: {p}" for p in check_style(root, part, whole)]
    problems += check_diagrams(root, whole)
    if problems:
        print("verify: FAIL\n" + "\n".join(f"- {p}" for p in problems), file=sys.stderr)
        return 1
    print("verify: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
