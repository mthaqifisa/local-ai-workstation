"""Writer steering: test-file loops are caught from LAW's activity summaries (2026-11-23 checklist PDF)."""

from __future__ import annotations

import pytest

from law.handlers.tasks import DOC_PROBE, WEB_DIG


@pytest.mark.parametrize("summary,probe", [
    ("▶ running cat > t17.typ <<'EOF'", True),
    ("▶ running cat > ~/LAW/cache/tmp/opencode/typst-test/t7.typ <<'EOF'", True),
    ("✏️ writing ~/LAW/outputs/agents/x/test3.md", True),
    ("✏️ editing ~/LAW/outputs/agents/x/house-1-week-checklist.typ", False),
    ("▶ running typst compile house-1-week-checklist.typ house-1-week-checklist.pdf", False),
    ("📖 reading ~/LAW/cache/tmp/opencode/typst-test/t8-1.png", False),
    ("✏️ writing ~/RnD/digital-bank/book/07-ledger.md", False),
    ("✏️ writing ~/LAW/cache/tmp/opencode/digital-bank-research/pr-2021-07-02-29-applications.txt", False),
    ("✏️ writing ~/LAW/cache/tmp/opencode/research/framework.pdf", False),
    ("✏️ writing ~/LAW/cache/tmp/opencode/typst-test/cover.typ", True),
])
def test_doc_probe_spots_throwaway_test_files(summary: str, probe: bool) -> None:
    assert bool(DOC_PROBE.search(summary)) is probe


# 2026-11-24 DBCT-4: 70 min getting around websites while the Act was already in sources/ (real summaries, shortened)
@pytest.mark.parametrize("summary,dig", [
    ("▶ running node -e ' ; const crypto = require(\"crypto\"); ; const https = require(\"https\");", True),
    ("▶ running curl -sS --max-time 20 -A \"Mozilla/5.0\" \"https://lom.agc.gov.my/js/responseCrypto.js\"", True),
    ("▶ running curl -sS --max-time 90 \"https://web.archive.org/cdx/search/cdx?url=pidm.com.my*\"", True),
    ("▶ running curl -sS --max-time 60 \"http://archive.org/wayback/available?url=pidm.com.my/en/\"", True),
    ("▶ running grep -o \"<loc>[^<]*</loc>\" bnm-sitemap.xml | head", True),
    ("▶ running curl -sS --max-time 60 -A \"Mozilla/5.0\" -e \"https://lom.agc.gov.my/principal.php\" x", True),
    # normal research: a plain fetch (with a browser user agent), converting and searching saved sources
    ("▶ running curl -sS -L --max-time 30 -A \"Mozilla/5.0 (Macintosh)\" \"https://www.bnm.gov.my/x\" -o a.pdf", False),
    ("▶ running pdftotext -layout sources/mdic-act-2011-act720-en.pdf sources/mdic-act-2011-act720-en.txt", False),
    ("▶ running grep -n -i \"coverage\\|insured\" sources/mdic-act-2011-act720-en.txt", False),
    ("▶ running curl -sS -L https://www.pidm.gov.my/en/deposit-insurance -o sources/pidm.html", False),
])
def test_web_dig_spots_getting_around_a_site(summary: str, dig: bool) -> None:
    assert bool(WEB_DIG.search(summary)) is dig
