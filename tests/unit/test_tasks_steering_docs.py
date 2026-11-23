"""Writer steering: test-file loops are caught from LAW's activity summaries (2026-11-23 checklist PDF)."""

from __future__ import annotations

import pytest

from law.handlers.tasks import DOC_PROBE


@pytest.mark.parametrize("summary,probe", [
    ("▶ running cat > t17.typ <<'EOF'", True),
    ("▶ running cat > ~/LAW/cache/tmp/opencode/typst-test/t7.typ <<'EOF'", True),
    ("✏️ writing ~/LAW/outputs/agents/x/test3.md", True),
    ("✏️ editing ~/LAW/outputs/agents/x/house-1-week-checklist.typ", False),
    ("▶ running typst compile house-1-week-checklist.typ house-1-week-checklist.pdf", False),
    ("📖 reading ~/LAW/cache/tmp/opencode/typst-test/t8-1.png", False),
    ("✏️ writing ~/RnD/digital-bank/book/07-ledger.md", False),
])
def test_doc_probe_spots_throwaway_test_files(summary: str, probe: bool) -> None:
    assert bool(DOC_PROBE.search(summary)) is probe
