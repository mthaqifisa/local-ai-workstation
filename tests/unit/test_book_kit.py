"""LAW's book kit (templates/rnd): installer, diagram sizing and the checks agents rely on."""

from __future__ import annotations

import importlib.util
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from types import ModuleType

import pytest

from law.book_kit import KIT_MARK, install

KIT = Path(__file__).parents[2] / "templates" / "rnd"
sys.dont_write_bytecode = True  # importing the kit's tools must not leave tools/__pycache__ in the template


def _load(name: str) -> ModuleType:
    sys.path.insert(0, str(KIT / "tools"))
    try:
        spec = importlib.util.spec_from_file_location(f"kit_{name}", KIT / "tools" / f"{name}.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(KIT / "tools"))


prepare, verify = _load("prepare"), _load("verify")


def _png(path: Path, w: int, h: int) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\xff" * w for _ in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_install_fills_metadata_and_never_overwrites(tmp_path: Path) -> None:
    added = install(KIT, tmp_path, 'Build a "Digital" Bank')
    assert {"book.md", "tools/build.sh", "tools/verify.py", ".law/check.sh", ".law/STYLE.md"} <= set(added)
    assert 'title: "Build a \\"Digital\\" Bank"' in (tmp_path / "book.md").read_text()
    assert (tmp_path / "chapters").is_dir() and (tmp_path / "diagrams").is_dir()
    (tmp_path / "tools" / "build.sh").write_text("# changed by the agent")
    assert install(KIT, tmp_path, "Other") == []  # the kit is there: nothing is replaced
    assert (tmp_path / "tools" / "build.sh").read_text() == "# changed by the agent"
    assert (tmp_path / KIT_MARK).is_file()


def test_mermaid_size_comes_from_the_svg(tmp_path: Path) -> None:
    (tmp_path / "diagrams").mkdir()
    (tmp_path / "build" / "diagrams").mkdir(parents=True)
    (tmp_path / "diagrams" / "wide.mmd").write_text("flowchart LR")
    _png(tmp_path / "diagrams" / "wide.png", 2352, 93)  # mmdc shrank it to 800 px: the PNG hides the real width
    (tmp_path / "build" / "diagrams" / "wide.svg").write_text('<svg viewBox="0 0 2100 31">')
    w, _, font = prepare.natural_pt(tmp_path / "diagrams" / "wide.png")
    assert w == 2100 * 0.75 and font == 12.0
    problems = verify.check_images("![Wide](diagrams/wide.png)", tmp_path)
    assert any("too wide" in p for p in problems)


def test_graphviz_size_and_tall_diagram(tmp_path: Path) -> None:
    (tmp_path / "diagrams").mkdir()
    (tmp_path / "diagrams" / "tall.dot").write_text("digraph {}")
    _png(tmp_path / "diagrams" / "tall.png", 300, 3000)  # 100 x 1000 pt at 216 dpi
    assert prepare.width_pct(tmp_path / "diagrams" / "tall.png") == round(100 / prepare.TEXT_W_PT * 100)
    assert any("too tall" in p for p in verify.check_images("![Tall](diagrams/tall.png)", tmp_path))


def test_small_diagram_keeps_natural_size(tmp_path: Path) -> None:
    (tmp_path / "diagrams").mkdir()
    (tmp_path / "diagrams" / "s.dot").write_text("digraph {}")
    _png(tmp_path / "diagrams" / "s.png", 600, 300)
    out = prepare.prepare("![Small](diagrams/s.png){#fig:s}", tmp_path)
    assert out == "![Small](diagrams/s.png){#fig:s width=42%}"
    assert verify.check_images("![Small](diagrams/s.png)", tmp_path) == []


def test_citations_need_links_and_unique_labels() -> None:
    text = ("Claim.[^a] Other.[^b] Ghost.[^c]\n\n[^a]: BNM, *Framework*, 2020. https://www.bnm.gov.my/x\n"
            "[^b]: A report, 2024, page 4.\n[^a]: again https://x.y\n")
    problems = verify.check_citations(text)
    assert any("[^b] has no link" in p for p in problems)
    assert any("[^c] is used but never defined" in p for p in problems)
    assert any("[^a] is defined twice" in p for p in problems)


def test_long_code_lines_fail_and_prose_does_not() -> None:
    long = "x" * 120
    assert verify.check_code(f"{long}\n```go\nshort()\n```\n") == []
    assert verify.check_code(f"```go\n{long}\n```\n")


def test_figures_are_numbered_alike_for_pdf_and_html() -> None:
    """Seen 2026-11-24: `@fig:x`{=typst} read "drawn in Figure 1" in the PDF and "drawn in : the" in the HTML."""
    text = ("See @fig:b and `@fig:a`{=typst}.\n\n![First](diagrams/a.png){#fig:a}\n\nInline ![x](i.png) no.\n\n"
            "![Second](diagrams/b.png){#fig:b width=50%}\n")
    assert prepare.figure_numbers(text) == {"fig:a": 1, "fig:b": 2}
    pdf, html = prepare.number_figures(text, "pdf"), prepare.number_figures(text, "html")
    assert pdf.startswith("See [Figure\u00a02](#fig:b) and [Figure\u00a01](#fig:a).") and "![First]" in pdf
    assert "![Figure\u00a01: First](diagrams/a.png)" in html and "![Figure\u00a02: Second]" in html
    assert "![x](i.png)" in html  # an inline image is not a numbered figure


def test_unknown_figure_references_and_raw_markup_fail() -> None:
    text = "See @fig:gone and `#pagebreak()`{=typst}.\n\n![A](diagrams/a.png){#fig:a}\n\nAlso `@fig:a`{=typst}.\n"
    problems = verify.check_references(text)
    assert any("@fig:gone refers to no figure" in p for p in problems)
    assert any("raw format-only markup '`#pagebreak()`{=typst}'" in p for p in problems)
    assert len(problems) == 2  # the old figure-reference form is translated, not reported


def test_missing_and_uncaptioned_images(tmp_path: Path) -> None:
    problems = verify.check_images("![](diagrams/none.png)", tmp_path)
    assert problems == ["image diagrams/none.png does not exist"]


TOOLS = all(shutil.which(t) for t in ("pandoc", "typst", "pdfinfo", "dot"))


@pytest.mark.skipif(not TOOLS, reason="needs pandoc, typst, poppler and graphviz (installed by LAW)")
def test_end_to_end_build_and_gate(tmp_path: Path) -> None:
    install(KIT, tmp_path, "Kit Test")
    (tmp_path / "chapters" / "01-one.md").write_text(
        "# One\n\nA claim.[^ch1-x] See @fig:flow.\n\n![Flow](diagrams/flow.png){#fig:flow}\n\n"
        "[^ch1-x]: Example, *Doc*, 2024. https://example.com/doc\n")
    (tmp_path / "diagrams" / "flow.dot").write_text("digraph { a -> b -> c }")
    ok = subprocess.run(["bash", ".law/check.sh"], cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "verify: OK" in ok.stdout
    html = (tmp_path / "book.html").read_text()
    assert '<a href="#fig:flow">Figure\u00a01</a>' in html and "Figure\u00a01: Flow" in html and "@fig" not in html
    (tmp_path / "chapters" / "02-two.md").write_text("# Two\n\nUnlinked.[^ch2-y]\n\n[^ch2-y]: A report.\n")
    bad = subprocess.run(["bash", ".law/check.sh"], cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert bad.returncode != 0 and "[^ch2-y] has no link" in bad.stderr


def test_install_skips_caches_and_system_files(tmp_path: Path) -> None:
    """Seen 2026-11-25: the kit's tests left tools/__pycache__ in the template; copied into a new project, git refused
    to add the ignored folder and the project failed to start."""
    kit = tmp_path / "kit"
    shutil.copytree(KIT, kit, ignore=shutil.ignore_patterns("__pycache__"))
    (kit / "tools" / "__pycache__").mkdir()
    (kit / "tools" / "__pycache__" / "prepare.cpython-312.pyc").write_bytes(b"x")
    (kit / ".DS_Store").write_bytes(b"x")
    added = install(kit, tmp_path / "book", "Title")
    assert "tools/verify.py" in added
    assert not any("__pycache__" in a or a.endswith((".pyc", ".DS_Store")) for a in added)
    assert not (tmp_path / "book" / "tools" / "__pycache__").exists()


def test_self_numbered_headings_turn_automatic_numbering_off() -> None:
    """Seen 2026-11-26: headings read "1.1 7.1 Double-entry design" because the writer and the kit both numbered."""
    text = "# 7. The core ledger\n\n## 7.1 Double-entry\n\n### Why twice\n\n```\n# 1 a comment\n```\n"
    out = prepare.unnumber_headings(text)
    assert "## 7.1 Double-entry {.unnumbered}" in out and "### Why twice {.unnumbered}" in out
    assert "# 1 a comment\n" in out  # code is not a heading
    assert prepare.unnumber_headings("# Intro\n\n## Background\n") == "# Intro\n\n## Background\n"


def test_readability_rules_for_every_reader(tmp_path: Path) -> None:
    """Seen 2026-11-26: the tech trial's prose averaged 40 words a sentence with 21 dashes per 1,000 words."""
    install(KIT, tmp_path, "Kit Test")
    long = " ".join(["word"] * 45) + "."
    quote = '"' + " ".join(["said"] * 35) + '"'
    (tmp_path / "chapters" / "01-one.md").write_text(
        f"# One\n\n## 1.1 Part\n\nShort and clear. {long}\n\nIt said {quote} once.\n\n"
        '1. See "The hold\'s twin" and 2. "The hold\'s code" again.\n\n```\n' + long + "\n```\n")
    problems = verify.check_style(tmp_path, tmp_path / "chapters" / "01-one.md", whole=True)
    assert any("sentences over 40 words" in p for p in problems)
    assert sum("quotation" in p for p in problems) == 1  # the short quoted titles never pair up
    dashes = "\n\n".join("A — b — c — d — e — f — g — h — i — j." for _ in range(40))
    (tmp_path / "chapters" / "01-one.md").write_text(f"# One\n\n{dashes}\n")
    assert any("dashes" in p for p in verify.check_style(tmp_path, tmp_path / "chapters" / "01-one.md", whole=True))


def test_the_gate_checks_only_the_writing_this_change_adds(tmp_path: Path) -> None:
    install(KIT, tmp_path, "Kit Test")
    chapter = tmp_path / "chapters" / "01-one.md"
    old = " ".join(["old"] * 45) + "."
    chapter.write_text(f"# One\n\n{old}\n")
    for args in (["init", "-q"], ["config", "user.email", "law@example.com"], ["config", "user.name", "LAW"],
                 ["add", "-A"], ["commit", "-q", "-m", "start"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    assert verify.check_style(tmp_path, chapter, whole=False) == []  # old text: the editor's job, not this ticket's
    chapter.write_text(f"# One\n\n{old}\n\n" + " ".join(["new"] * 45) + ".\n")
    found = verify.check_style(tmp_path, chapter, whole=False)
    assert len(found) == 1 and "45-word sentence" in found[0] and "new new" in found[0]


def test_sequence_self_messages_and_long_labels(tmp_path: Path) -> None:
    install(KIT, tmp_path, "Kit Test")
    (tmp_path / "diagrams" / "seq.mmd").write_text(
        "sequenceDiagram\n    J->>J: checks itself\n    J->>P: " + "x" * 70 + "\n")
    (tmp_path / "diagrams" / "states.mmd").write_text("stateDiagram-v2\n    held --> held: retried\n")
    problems = verify.check_diagrams(tmp_path, whole=True)
    assert len(problems) == 2 and "self-message" in problems[0] and "70-character label" in problems[1]


def test_upgrade_replaces_only_what_is_still_laws(tmp_path: Path) -> None:
    """Seen 2026-11-26: the tech trial kept the kit it started with while the kit improved."""
    from law import book_kit

    old_kit = tmp_path / "old"
    shutil.copytree(KIT, old_kit, ignore=shutil.ignore_patterns("__pycache__"))
    (old_kit / "tools" / "build.sh").write_text("# old build\n")
    (old_kit / "tools" / "book.css").write_text("/* old css */\n")
    (old_kit / "tools" / "mermaid.json").unlink()  # a file the kit added later
    book = tmp_path / "book"
    install(old_kit, book, "Title")
    (book / "tools" / "book.css").write_text("/* the agent fixed a check */\n")  # an agent's change is kept
    changed = book_kit.upgrade(KIT, book)
    assert "tools/build.sh" in changed and (book / "tools" / "build.sh").read_text() == (KIT / "tools" / "build.sh").read_text()
    assert "tools/book.css" not in changed and "agent fixed" in (book / "tools" / "book.css").read_text()
    assert "tools/mermaid.json" in changed
    assert book_kit.upgrade(KIT, book) == []  # nothing left to do


def test_the_plans_readers_replace_the_default_list_once(tmp_path: Path) -> None:
    """Readers come from each document's plan (a cookbook's are not a bank's); the kit's list is only a default."""
    from law import book_kit

    install(KIT, tmp_path, "Kit Test")
    (tmp_path / ".law" / "PROMPT.md").write_text(
        "**What I'll write**: a cookbook.\n**Readers**:\n- a home cook, who wants recipes that work first time\n"
        "- a chef, who checks technique\n**Style**: warm.\n")
    assert book_kit.adopt_readers(tmp_path)
    style = (tmp_path / ".law" / "STYLE.md").read_text()
    assert "- a home cook, who wants recipes that work first time" in style and "- a chef" in style
    assert "decision-maker" not in style and book_kit.READERS_MARK not in style
    assert not book_kit.adopt_readers(tmp_path)  # once: the project's own list stays


def test_a_document_sets_its_own_readability_limits(tmp_path: Path) -> None:
    install(KIT, tmp_path, "Kit Test")
    assert verify.limits(tmp_path)["sentence-max"] == verify.SENTENCE_MAX
    book = tmp_path / "book.md"
    book.write_text(book.read_text().replace("---\n", "---\nsentence-max: 55\n", 1))
    chapter = tmp_path / "chapters" / "01-one.md"
    chapter.write_text("# One\n\n" + " ".join(["word"] * 50) + ".\n")
    assert verify.limits(tmp_path)["sentence-max"] == 55
    assert not any("sentences over" in p for p in verify.check_style(tmp_path, chapter, whole=True))
