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
