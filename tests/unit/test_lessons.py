"""Lessons carried between projects: parsed, kept once, newest first within the limit."""

from __future__ import annotations

from pathlib import Path

import pytest

from law.lessons import HEAD, LIMIT, Lessons, parse
from law.paths import LawPaths

ENTRY = "## {t}\ntags: typst 0.15\nUse #box for tick glyphs; the font lacks U+2610.\n"


def _project(root: Path, *titles: str) -> Path:
    (root / ".law").mkdir(parents=True, exist_ok=True)
    (root / ".law" / "LESSONS.md").write_text("# Lessons\n\n" + "".join(ENTRY.format(t=t) for t in titles))
    return root


def test_parse_skips_empty_entries() -> None:
    assert [x.title for x in parse("## A\nbody\n## B\n\n## C\ntext")] == ["A", "C"]


def test_harvest_once_and_prompt_newest_first(tmp_path: Path) -> None:
    store = Lessons(LawPaths(repo=tmp_path, home=tmp_path / "LAW", user_home=tmp_path))
    a = _project(tmp_path / "book-a", "Tick boxes in Typst", "mmdc needs the puppeteer config")
    assert store.harvest(a, "writer") == ["Tick boxes in Typst", "mmdc needs the puppeteer config"]
    assert store.harvest(a, "writer") == []  # already kept
    b = _project(tmp_path / "book-b", "tick boxes in typst", "Graphviz dpi for sharp PNGs")
    assert store.harvest(b, "writer") == ["Graphviz dpi for sharp PNGs"]  # same problem, other case: one lesson
    text = store.prompt("writer")
    assert text.startswith(HEAD) and text.index("Graphviz") < text.index("Tick boxes")
    assert store.prompt("coder") == ""


def test_prompt_stays_within_limit(tmp_path: Path) -> None:
    store = Lessons(LawPaths(repo=tmp_path, home=tmp_path / "LAW", user_home=tmp_path))
    _project(tmp_path / "p", *[f"Problem number {n}" for n in range(200)])
    store.harvest(tmp_path / "p", "coder")
    assert 0 < len(store.prompt("coder")) <= LIMIT


def test_role_names_are_checked(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Lessons(LawPaths(repo=tmp_path, home=tmp_path, user_home=tmp_path)).file("../x")
