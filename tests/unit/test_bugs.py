"""law.bugs: the findings file keeps LAW's format, and no finding is lost to an agent rewriting it."""

from __future__ import annotations

from pathlib import Path

from law import bugs


def test_a_findings_file_in_another_format_gets_laws_header(tmp_path: Path) -> None:
    file = tmp_path / ".law" / "BUGS.md"
    file.parent.mkdir()
    file.write_text("# Findings Log\n\n| BUG-id | Status |\n|---|---|\n")
    assert bugs.ensure_format(tmp_path) and file.read_text().startswith(bugs.HEADER)
    assert "## BUG-<next number> · open" in file.read_text()
    assert not bugs.ensure_format(tmp_path)  # already LAW's
    bugs.add(tmp_path, "a real finding")
    file.write_text("junk\n" + file.read_text())
    assert not bugs.ensure_format(tmp_path)  # it has a finding: never reset


def test_findings_removed_by_a_rewrite_are_put_back(tmp_path: Path) -> None:
    bugs.add(tmp_path, "first", "Severity: major\nEvidence: x")
    bugs.add(tmp_path, "second")
    before = bugs.path(tmp_path).read_text()
    bugs.path(tmp_path).write_text(bugs.HEADER + "\n## BUG-2 · verified · editor · now\nsecond\n")
    assert bugs.restore_lost(tmp_path, before) == ["BUG-1"]
    found = {b.id: b for b in bugs.load(tmp_path)}
    assert found["BUG-2"].status == "verified" and found["BUG-1"].title == "first"
    assert "Evidence: x" in found["BUG-1"].body
    assert bugs.restore_lost(tmp_path, before) == []


def test_a_finding_the_fix_stage_never_marked_fixed_stays_open(tmp_path: Path) -> None:
    """Seen 2026-11-25: the editor verified six findings in its retests; the chapter had not changed since."""
    (tmp_path / ".law").mkdir()
    before = (bugs.HEADER + "\n## BUG-1 · open · editor · 2026-11-25 10:00\nWrong quote\nSeverity: major\n"
              "\n## BUG-2 · fixed · editor · 2026-11-25 10:05\nWrong figure\nSeverity: major\n"
              "\n## BUG-3 · reopened · editor · 2026-11-25 10:10\nWrong name\nSeverity: major")
    after = before.replace("BUG-1 · open", "BUG-1 · verified").replace("BUG-2 · fixed", "BUG-2 · verified") \
        .replace("BUG-3 · reopened", "BUG-3 · verified") + "\nRetest: now understood as plain language."
    bugs.path(tmp_path).write_text(after)
    assert bugs.keep_unfixed(tmp_path, before) == ["BUG-1", "BUG-3"]
    status = {b.id: b.status for b in bugs.load(tmp_path)}
    assert status == {"BUG-1": "open", "BUG-2": "verified", "BUG-3": "reopened"}  # a real fix is verified
    text = bugs.path(tmp_path).read_text()
    assert text.count("kept open") == 1 and text.count("kept reopened") == 1 and "plain language" in text
    assert bugs.keep_unfixed(tmp_path, bugs.path(tmp_path).read_text()) == []


def test_a_reviewer_rewriting_the_header_gets_it_back(tmp_path: Path) -> None:
    (tmp_path / ".law").mkdir()
    before = bugs.HEADER + "\n## BUG-1 · open · editor · 2026-11-25 10:00\nWrong quote\n"
    bugs.path(tmp_path).write_text(before.replace("or reopened.", "or reopened. Only QA sets verified or reopened.")
                                   + "\n## BUG-2 · open · editor · 2026-11-25 10:30\nNew one\n")
    assert bugs.keep_preamble(tmp_path, before)
    text = bugs.path(tmp_path).read_text()
    assert text.startswith(bugs.HEADER) and text.count("Only QA sets") == 1 and "BUG-2" in text
    assert not bugs.keep_preamble(tmp_path, text)
