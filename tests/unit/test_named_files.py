"""The planner sees the text of files the request names (2026-11-24: a /rnd plan ignored the brief it pointed to)."""

from __future__ import annotations

from pathlib import Path

from law.handlers.project import DOC_PLAN, NAMED_MAX_CHARS, named_files


def test_home_relative_brief_is_inlined(tmp_path: Path) -> None:
    (tmp_path / "RnD" / "book").mkdir(parents=True)
    (tmp_path / "RnD" / "book" / "BRIEF.md").write_text("Readers: a CTO and a reader with no tech background.")
    out = named_files("The full brief is ~/RnD/book/BRIEF.md; read it.", home=tmp_path)
    assert "--- ~/RnD/book/BRIEF.md ---" in out and "a CTO and a reader" in out


def test_trailing_punctuation_and_duplicates(tmp_path: Path) -> None:
    (tmp_path / "spec.txt").write_text("spec body")
    out = named_files("See ~/spec.txt. Again: ~/spec.txt, and (~/spec.txt)!", home=tmp_path)
    assert out.count("spec body") == 1


def test_missing_binary_and_none_give_nothing(tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    assert named_files("no files here", home=tmp_path) == ""
    assert named_files("~/missing.md and ~/pic.png", home=tmp_path) == ""


def test_long_file_is_cut_with_a_note(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * (NAMED_MAX_CHARS + 10))
    out = named_files("~/big.md", home=tmp_path)
    assert "read it in full while working" in out and out.count("x") >= NAMED_MAX_CHARS


def test_doc_plan_forbids_facts_and_follows_the_request() -> None:
    assert "State no facts, figures, dates" in DOC_PLAN and "word for word" in DOC_PLAN


def test_kit_conventions_reach_the_document_planner(tmp_path: Path) -> None:
    from law.handlers.project import kit_conventions
    assert kit_conventions(tmp_path) == ""
    (tmp_path / ".law").mkdir()
    (tmp_path / ".law" / "STYLE.md").write_text("Each chapter is its own file in chapters/.")
    out = kit_conventions(tmp_path)
    assert "no ticket integrates, assembles or rebuilds" in out and "chapters/" in out
