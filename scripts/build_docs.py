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
# Link previews (WhatsApp, Telegram, LinkedIn, X, Slack, iMessage): the published page and the repo's social image.
SITE_URL = "https://mthaqifisa.github.io/local-ai-workstation/"
SOCIAL_IMAGE = "https://repository-images.githubusercontent.com/1271122443/bd3e971d-e4f9-4686-9edd-433557b61d02"
SITE_TITLE = "LAW: a private AI team on a single Mac"
SITE_DESCRIPTION = ("Open-source AI workstation for Apple Silicon: agents that plan, build and test software, "
                    "research, write and operate your Mac, plus images, video, speech and music, on open models "
                    "running locally.")
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
MERMAID = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
DIAGRAMS = "docs/images/diagrams"  # Mermaid blocks with no static copy, rendered here by build() and committed
MERMAID_WITH_STATIC = re.compile(r"```mermaid\n.*?```\s*\n\s*\*\(Static image: \[[^\]]*\]\(([^)\s]+)\)\)\*",
                                 re.DOTALL)


def combined(repo: Path = REPO) -> str:
    parts = [(repo / LANDING).read_text()]
    for name, anchor in SOURCES:
        raw = MERMAID_WITH_STATIC.sub(lambda m: f"![]({m.group(1)})", (repo / name).read_text())
        text = MERMAID.sub(lambda m: _rendered(repo, m.group(1)), relink(raw, name))  # paths from the repo root
        lines = text.splitlines()
        title = TITLES.get(name) or (lines[0].lstrip("# ").strip() if lines and lines[0].startswith("# ") else name)
        body = "\n".join(lines[1:] if lines and lines[0].startswith("# ") else lines)
        parts.append(f"# {title} {{#{anchor}}}\n\n{body}\n")  # its `##` sections stay second level, in the menu
    return "\n".join(parts)


def diagram_path(source: str) -> str:
    """Where a Mermaid block's rendered image lives: named by its source, so an edited diagram gets a new image."""
    return f"{DIAGRAMS}/{hashlib.sha256(source.encode()).hexdigest()[:16]}.png"


def _rendered(repo: Path, source: str) -> str:
    image = diagram_path(source)
    return f"![]({image})" if (repo / image).is_file() else f"```mermaid\n{source}```"  # code until rendered


def render_diagrams(repo: Path = REPO) -> list[str]:
    """Render every Mermaid block without a static copy to a PNG (mmdc, via LAW's headless Chrome), and remove
    images no block uses any more. Returns the images written."""
    wanted: dict[str, str] = {}
    for name, _ in SOURCES:
        text = MERMAID_WITH_STATIC.sub("", (repo / name).read_text())
        for m in MERMAID.finditer(text):
            wanted[diagram_path(m.group(1))] = m.group(1)
    folder = repo / DIAGRAMS
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.png"):
        if f"{DIAGRAMS}/{old.name}" not in wanted:
            old.unlink()
    written = []
    puppeteer = Path.home() / "LAW" / "config" / "puppeteer.json"
    config = repo / "templates" / "rnd" / "tools" / "mermaid.json"
    for image, source in wanted.items():
        if (repo / image).is_file():
            continue
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "diagram.mmd"
            src.write_text(source)
            cmd = ["mmdc", "-i", str(src), "-o", str(repo / image), "-s", "2", "-b", "white", "-q", "-c", str(config)]
            if puppeteer.is_file():
                cmd += ["-p", str(puppeteer)]
            subprocess.run(cmd, check=True)
        written.append(image)
    return written


def social_meta() -> str:
    """Open Graph and Twitter card tags, so a shared link shows the title, a summary and the image."""
    tags = {
        "description": SITE_DESCRIPTION,
        "og:type": "website", "og:url": SITE_URL, "og:site_name": "LAW", "og:title": SITE_TITLE,
        "og:description": SITE_DESCRIPTION, "og:image": SOCIAL_IMAGE, "og:image:alt": SITE_TITLE,
        "twitter:card": "summary_large_image", "twitter:title": SITE_TITLE,
        "twitter:description": SITE_DESCRIPTION, "twitter:image": SOCIAL_IMAGE,
    }
    lines = []
    for key, value in tags.items():
        attr = "property" if key.startswith("og:") else "name"
        value = value.replace("&", "&amp;").replace('"', "&quot;")
        lines.append(f'<meta {attr}="{key}" content="{value}">')
    return "\n".join(lines) + f'\n<link rel="canonical" href="{SITE_URL}">\n'


def build(repo: Path = REPO) -> Path:
    render_diagrams(repo)  # first: the page's hash covers the images it embeds
    out = repo / OUT
    scratch = Path.home() / "LAW" / "cache" / "tmp"  # LAW's temp folder when this Mac runs LAW, else the system's
    with tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None) as tmp:
        md = Path(tmp) / "docs.md"
        md.write_text(combined(repo))
        header = Path(tmp) / "head.html"
        header.write_text(f'<meta name="law-docs-source" content="{source_hash(repo)}">\n' + social_meta())
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
