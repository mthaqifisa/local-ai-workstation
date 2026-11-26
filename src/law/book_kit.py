"""LAW's book kit (templates/rnd): the tested build, checks and style guide every document project starts with.

Seen 2026-11-24: the first /rnd trial spent 99 minutes building a pandoc + Typst template before writing a word, and
every new document project would have paid that again. The kit is built and tested once, in the repo: book.md
(metadata) + chapters/*.md -> book.pdf (A4) and book.html (one file), diagrams rendered from Mermaid or Graphviz,
and .law/check.sh, which fails on unreadable or over-tall diagrams, citations without a link and code lines too long
for the page.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

MANIFEST = ".law/kit.json"  # hashes of the kit files as LAW last wrote them: a file that still matches is LAW's
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
    manifest = {rel: _digest(folder / rel) for rel in added if rel.startswith("tools/")}
    (folder / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return added


def upgrade(kit: Path, folder: Path) -> list[str]:
    """Bring a project's copy of the kit up to date (seen 2026-11-26: the kit learned readability checks, diagram
    wrapping and self-numbered headings while the tech trial ran on the copy it started with). A tools/ file is
    replaced only while it is still LAW's: it matches the hash LAW recorded, or, before LAW recorded hashes, git has
    exactly one commit of it (the kit's own). An agent's change is kept. .law/STYLE.md gains the kit's sections it
    lacks; .gitignore gains the kit's lines it lacks. Returns the files changed."""
    if not (folder / KIT_MARK).exists():
        return []
    try:
        recorded: dict[str, str] = json.loads((folder / MANIFEST).read_text())
    except (OSError, ValueError):
        recorded = {}
    changed, manifest = [], dict(recorded)
    for src in kit_files(kit):
        rel = src.relative_to(kit).as_posix()
        dest = folder / rel
        if rel.startswith("tools/"):
            if dest.exists() and _digest(dest) != _digest(src) and not _still_laws(folder, rel, recorded):
                continue
            if not dest.exists() or _digest(dest) != _digest(src):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dest)  # a fresh time, so build.sh sees a newer config and re-renders
                changed.append(rel)
            manifest[rel] = _digest(src)
        elif rel == ".law/STYLE.md" and dest.exists():
            mine = dest.read_text()
            missing = [sec for sec in _sections(src.read_text()) if sec.splitlines()[0] not in mine]
            if missing:
                dest.write_text(mine.rstrip() + "\n\n" + "\n\n".join(missing) + "\n")
                changed.append(rel)
        elif rel == ".gitignore" and dest.exists():
            have = set(dest.read_text().splitlines())
            extra = [ln for ln in src.read_text().splitlines() if ln and ln not in have]
            if extra:
                dest.write_text(dest.read_text().rstrip() + "\n" + "\n".join(extra) + "\n")
                changed.append(rel)
    (folder / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return changed


READERS_MARK = "<!-- readers: LAW replaces this list"
READERS_PART = re.compile(r"\*\*Readers\*\*:?\s*(.*?)(?=\n\*\*[A-Z][^*]{0,40}\*\*|\Z)", re.DOTALL)


def adopt_readers(folder: Path) -> bool:
    """Put the approved plan's **Readers** (.law/PROMPT.md) into .law/STYLE.md in place of the kit's default list,
    once: a list already written for the project stays. True when STYLE.md changed."""
    style, plan = folder / ".law" / "STYLE.md", folder / ".law" / "PROMPT.md"
    try:
        text, agreed = style.read_text(), plan.read_text()
    except OSError:
        return False
    found = READERS_PART.search(agreed)
    if READERS_MARK not in text or not found:
        return False
    readers = [ln.strip() for ln in found.group(1).splitlines() if ln.strip().startswith(("- ", "* "))]
    if not readers:
        return False
    start = text.index(READERS_MARK)
    end = text.index("\n\n", text.index("\n- ", start)) if "\n- " in text[start:] else len(text)
    block = ("The readers this document is written for, and what each needs (from the approved plan):\n"
             + "\n".join("- " + r[2:].strip() for r in readers))
    style.write_text(text[:start] + block + text[end:])
    return True


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _still_laws(folder: Path, rel: str, recorded: dict[str, str]) -> bool:
    if rel in recorded:
        return _digest(folder / rel) == recorded[rel]
    log = subprocess.run(["git", "log", "--format=%H", "--", rel], cwd=folder, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=folder, capture_output=True, text=True)
    return log.returncode == 0 and len(log.stdout.split()) == 1 and not dirty.stdout.strip()


def _sections(text: str) -> list[str]:
    """The `## ` sections of a Markdown file, each with its heading."""
    parts = text.split("\n## ")
    return ["## " + part.strip() for part in parts[1:]]


def _yaml(text: str) -> str:
    """A value safe inside a double-quoted YAML string."""
    return " ".join(text.split()).replace("\\", "\\\\").replace('"', '\\"')
