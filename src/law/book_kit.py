"""LAW's book kit (templates/rnd): the tested build, checks and style guide every document project starts with.

Seen 2026-11-24: the first /rnd trial spent 99 minutes building a pandoc + Typst template before writing a word, and
every new document project would have paid that again. The kit is built and tested once, in the repo: book.md
(metadata) + chapters/*.md -> book.pdf (A4) and book.html (one file), diagrams rendered from Mermaid or Graphviz,
and .law/check.sh, which fails on unreadable or over-tall diagrams, citations without a link and code lines too long
for the page.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

KIT_MARK = "tools/verify.py"  # a folder that has it already has the kit (never overwrite an agent's changes)
# Not part of the kit: Python's cache from running the kit's tests, and macOS folder files. Copied, git refused to
# add them (`tools/__pycache__` is ignored) and the project failed to start (seen 2026-11-25).
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_FILES = {".DS_Store"}


def kit_files(kit: Path) -> list[Path]:
    return sorted(p for p in kit.rglob("*") if p.is_file() and p.name not in SKIP_FILES
                  and p.suffix != ".pyc" and not SKIP_DIRS & set(p.relative_to(kit).parts))


def install(kit: Path, folder: Path, title: str, subtitle: str = "") -> list[str]:
    """Copy the kit into a document project's folder: files that already exist are kept. Returns the files added."""
    if (folder / KIT_MARK).exists():
        return []
    added = []
    for src in kit_files(kit):
        rel = src.relative_to(kit)
        dest = folder / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rel.as_posix() == "book.md":
            dest.write_text(src.read_text().format(title=_yaml(title), subtitle=_yaml(subtitle),
                                                   date=time.strftime("%-d %B %Y")))
        else:
            shutil.copy2(src, dest)
        added.append(rel.as_posix())
    for name in ("chapters", "diagrams", "assets"):
        (folder / name).mkdir(exist_ok=True)
    return added


def _yaml(text: str) -> str:
    """A value safe inside a double-quoted YAML string."""
    return " ".join(text.split()).replace("\\", "\\\\").replace('"', '\\"')
