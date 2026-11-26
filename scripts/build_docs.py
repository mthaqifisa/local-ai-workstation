"""Build docs/index.html: the product page (docs/landing.md) and the full documentation (README, quick start,
runbook, architecture) on one self-contained page with a side menu of every section and embedded screenshots.

The page is generated, never edited by hand, so it cannot drift from the Markdown it shows. It records a hash of
its sources; tests/unit/test_docs_page.py fails when a source changed and the page was not rebuilt.

Usage: python3 scripts/build_docs.py          (needs pandoc)
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LANDING = "docs/landing.md"  # the product page: its own sections, kept as written
SOURCES = [  # (file, the section's anchor on the page); each one becomes a top-level section under its title
    ("README.md", "overview"),
    ("docs/QUICKSTART.md", "quick-start"),
    ("docs/RUNBOOK.md", "runbook"),
    ("docs/ARCHITECTURE.md", "architecture-in-depth"),  # the README has its own "Architecture"
]
TITLES = {"README.md": "Overview"}  # the menu name where a document's own title reads oddly on the page
CSS = "docs/docs.css"
OUT = "docs/index.html"
HASH_META = re.compile(r'<meta name="law-docs-source" content="([0-9a-f]{64})"')


def source_hash(repo: Path = REPO) -> str:
    """One hash over every source and the stylesheet: the page is current when it carries this value."""
    digest = hashlib.sha256()
    images = sorted(p.relative_to(repo).as_posix() for p in (repo / "docs" / "images").rglob("*.png"))
    for rel in [LANDING, *(name for name, _ in SOURCES), CSS, "scripts/build_docs.py", *images]:
        digest.update(rel.encode() + b"\0" + (repo / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def relink(text: str, source: str) -> str:
    """Links between the sources become in-page anchors; links to other repo files stay relative to docs/; images are
    resolved from the repo root so pandoc can embed them."""
    anchors = {name: anchor for name, anchor in SOURCES}
    base = Path(source).parent

    def fix(m: re.Match[str]) -> str:
        bang, label, target = m.group(1), m.group(2), m.group(3)
        if re.match(r"^(https?:|mailto:|#)", target):
            return m.group(0)
        path, _, frag = target.partition("#")
        rel = (base / path).as_posix() if path else source
        rel = str(Path(rel)).replace("\\", "/")
        if bang:
            return f"![{label}]({(REPO / rel).as_posix()})"
        if rel in anchors:
            return f"[{label}](#{frag or anchors[rel]})"
        if rel == OUT:
            return f"[{label}](#overview)"
        target_from_docs = Path("..", rel).as_posix() if not rel.startswith("docs/") else rel[len("docs/"):]
        return f"[{label}]({target_from_docs}{'#' + frag if frag else ''})"  # the page lives in docs/

    return re.sub(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)", fix, text)


# GitHub draws the README's Mermaid diagrams; a browser opening this file would show their source, so each one is
# replaced by the static copy the README links right after it.
MERMAID_WITH_STATIC = re.compile(r"```mermaid\n.*?```\s*\n\s*\*\(Static image: \[[^\]]*\]\(([^)\s]+)\)\)\*",
                                 re.DOTALL)


def combined(repo: Path = REPO) -> str:
    parts = [(repo / LANDING).read_text()]
    for name, anchor in SOURCES:
        raw = MERMAID_WITH_STATIC.sub(lambda m: f"![]({m.group(1)})", (repo / name).read_text())
        text = relink(raw, name)
        lines = text.splitlines()
        title = TITLES.get(name) or (lines[0].lstrip("# ").strip() if lines and lines[0].startswith("# ") else name)
        body = "\n".join(lines[1:] if lines and lines[0].startswith("# ") else lines)
        parts.append(f"# {title} {{#{anchor}}}\n\n{body}\n")  # its `##` sections stay second level, in the menu
    return "\n".join(parts)


def build(repo: Path = REPO) -> Path:
    out = repo / OUT
    scratch = Path.home() / "LAW" / "cache" / "tmp"  # LAW's temp folder when this Mac runs LAW, else the system's
    with tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None) as tmp:
        md = Path(tmp) / "docs.md"
        md.write_text(combined(repo))
        header = Path(tmp) / "head.html"
        header.write_text(f'<meta name="law-docs-source" content="{source_hash(repo)}">\n')
        subprocess.run(["pandoc", str(md), "-f", "markdown", "-t", "html5", "-s", "--toc", "--toc-depth=2",
                        "--embed-resources", "--resource-path", str(repo), "--css", str(repo / CSS),
                        "--include-in-header", str(header), "--metadata", "title=LAW",
                        "--metadata", "pagetitle=LAW: a private AI team on a single Mac",
                        "--metadata", "toc-title=On this page", "-o", str(out)],
                       check=True, cwd=repo)  # cwd: the landing's screenshots are paths from the repo root
    return out


def is_current(repo: Path = REPO) -> bool:
    page = repo / OUT
    if not page.is_file():
        return False
    found = HASH_META.search(page.read_text(errors="replace"))
    return found is not None and found.group(1) == source_hash(repo)


if __name__ == "__main__":
    print(f"wrote {build().relative_to(REPO)}")
    sys.exit(0)
