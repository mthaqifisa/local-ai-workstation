"""docs/index.html is generated from the Markdown docs and must be rebuilt when they change."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).parents[2]


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("build_docs", REPO / "scripts" / "build_docs.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_docs"] = mod
    spec.loader.exec_module(mod)
    return mod


build_docs = _load()


def test_docs_page_is_current() -> None:
    assert build_docs.is_current(), "docs/index.html is out of date: run python3 scripts/build_docs.py"


def test_links_between_sources_become_anchors_and_others_stay_relative() -> None:
    text = "[Runbook](docs/RUNBOOK.md) [ADRs](docs/adr/) [Research](RESEARCH.md) [web](https://example.com)"
    out = build_docs.relink(text, "README.md")
    assert "[Runbook](#runbook)" in out and "[ADRs](adr)" in out and "[Research](../RESEARCH.md)" in out
    assert "[web](https://example.com)" in out
    assert "[Quick start](#quick-start)" in build_docs.relink("[Quick start](QUICKSTART.md)", "docs/RUNBOOK.md")


def test_each_source_is_one_top_level_section() -> None:
    text = build_docs.combined()
    for _, anchor in build_docs.SOURCES:
        assert f"{{#{anchor}}}" in text
    for anchor in ("why-law", "proof", "capabilities", "how-it-works", "screenshots", "get-started"):
        assert f"{{#{anchor}}}" in text  # the product page's sections come first, in the side menu
    assert "\n# Models" not in text  # the README's own sections moved one level down


def test_every_diagram_is_an_image_not_mermaid_source() -> None:
    """Seen 2026-11-26: the architecture section showed Mermaid source as text, since only GitHub draws it."""
    text = build_docs.combined()
    assert "```mermaid" not in text, "a Mermaid block has no rendered image: run python3 scripts/build_docs.py"
    assert build_docs.diagram_path("flowchart LR\n a --> b\n").startswith("docs/images/diagrams/")
