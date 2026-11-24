"""Size every rendered diagram for the page: its natural size, never wider than the text column.

A diagram's natural size: Mermaid's from the SVG tools/build.sh keeps in build/diagrams/ (CSS px x 0.75 pt; the
PNG cannot tell, because mmdc shrinks a PNG wider than 800 px), Graphviz's from its PNG (rendered at 216 dpi, 3 px
per point). A diagram
narrower than the column keeps its natural size (no blown-up text); a wider one is scaled down to the column.
tools/verify.py fails the build when that scaling would make its text unreadable: split such a diagram.
Figures are numbered here, in order, the same way for the PDF and the HTML: a captioned image with
{#fig:<name>} is Figure N, and the text refers to it as @fig:<name>, which becomes a link "Figure N" (an older
`@fig:<name>`{=typst} is read the same way). For the HTML, whose captions pandoc does not number, the caption is
prefixed "Figure N: "; Typst numbers its own captions in the same order.
The book is book.md (its metadata and any front matter) followed by chapters/*.md in name order
(01-intro.md, 02-licensing.md, ...).
Usage: python3 tools/prepare.py <project folder> build/book.render.md [pdf|html]
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

TEXT_W_PT = 470.6  # A4 (595.3 pt) minus 2 x 2.2 cm margins
DOT_PX_PER_PT = 3.0
PX_PT = 0.75  # one CSS pixel in points
FONT_PT = {".mmd": 12.0, ".dot": 14.0}  # label size at natural size: Mermaid's 16 px, Graphviz's 14 pt
VIEWBOX = re.compile(r'viewBox="[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)"')
IMAGE = re.compile(r"!\[(?P<cap>[^\]]*)\]\((?P<path>[^)\s]+)\)(?P<attrs>\{[^}]*\})?")
FIGURE = re.compile(r"^!\[(?P<cap>[^\]]+)\]\([^)\s]+\)(?:\{[^}]*\})?\s*$", re.MULTILINE)  # alone: a numbered figure
LABEL = re.compile(r"#(fig:[\w-]+)")
FIG_REF = re.compile(r"`?@(fig:[\w-]+)(?:`\{=\w+\})?")


def png_size(path: Path) -> tuple[int, int] | None:
    if not path.is_file():
        return None  # tools/verify.py reports it
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def source_of(path: Path) -> str:
    """The extension of the diagram source a rendered PNG came from (Mermaid when there is none)."""
    return ".dot" if path.with_suffix(".dot").is_file() else ".mmd"


def natural_pt(path: Path) -> tuple[float, float, float] | None:
    """A rendered diagram's natural width and height in points, and its label size in points."""
    if path.suffix.lower() != ".png":
        return None
    src = source_of(path)
    if src == ".mmd":
        svg = path.parents[1] / "build" / path.parent.name / path.with_suffix(".svg").name
        m = VIEWBOX.search(svg.read_text(errors="replace")) if svg.is_file() else None
        if m is None:
            return None  # not built from Mermaid here (or not built yet): tools/verify.py reports stale renders
        return float(m.group(1)) * PX_PT, float(m.group(2)) * PX_PT, FONT_PT[src]
    size = png_size(path)
    return None if size is None else (size[0] / DOT_PX_PER_PT, size[1] / DOT_PX_PER_PT, FONT_PT[src])


def width_pct(path: Path) -> int | None:
    nat = natural_pt(path)
    return None if nat is None else max(10, min(100, round(nat[0] / TEXT_W_PT * 100)))


def prepare(text: str, root: Path) -> str:
    def fix(m: re.Match[str]) -> str:
        attrs = m.group("attrs") or ""
        path = m.group("path")
        if "width=" in attrs or not path.startswith("diagrams/"):
            return m.group(0)
        pct = width_pct(root / path)
        if pct is None:
            return m.group(0)
        inner = attrs[1:-1].strip() if attrs else ""
        return f"![{m.group('cap')}]({path}){{{(inner + ' ') if inner else ''}width={pct}%}}"

    return IMAGE.sub(fix, text)


def figure_numbers(text: str) -> dict[str, int]:
    """Each figure label and its number: the captioned images standing alone, in order."""
    numbers: dict[str, int] = {}
    for n, m in enumerate(FIGURE.finditer(text), start=1):
        label = LABEL.search(m.group(0))
        if label:
            numbers[label.group(1)] = n
    return numbers


def number_figures(text: str, target: str) -> str:
    numbers = figure_numbers(text)

    def ref(m: re.Match[str]) -> str:
        n = numbers.get(m.group(1))
        return f"[Figure\u00a0{n}](#{m.group(1)})" if n else m.group(0)  # verify.py reports an unknown label

    text = FIG_REF.sub(ref, text)
    if target == "html":
        count = 0

        def caption(m: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return m.group(0).replace(f"![{m.group('cap')}]", f"![Figure\u00a0{count}: {m.group('cap')}]", 1)

        text = FIGURE.sub(caption, text)
    return text


def parts(root: Path) -> list[Path]:
    """book.md, then the chapters in order."""
    return [root / "book.md", *sorted((root / "chapters").glob("*.md"))]


def source_text(root: Path) -> str:
    return "\n\n".join(p.read_text().rstrip() for p in parts(root) if p.is_file()) + "\n"


def main() -> None:
    root, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    target = sys.argv[3] if len(sys.argv) > 3 else "pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(number_figures(prepare(source_text(root), root), target))


if __name__ == "__main__":
    main()
