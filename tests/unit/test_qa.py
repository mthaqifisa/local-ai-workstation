"""Black-box QA (ADR-0018): the bug list, the QA round inside a project review, `/bugfix` and the board's bug tickets."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from law import bugs
from law.paths import LawPaths

REPO = Path(__file__).resolve().parents[2]


def _ctx(payload: dict[str, Any]) -> Any:
    async def progress(_: str) -> None:
        return None

    return SimpleNamespace(job=SimpleNamespace(payload=payload, id="j1", parent="root"), progress=progress,
                           cancel_event=asyncio.Event())


def test_bug_list_numbers_parses_and_tracks_status(tmp_path: Path) -> None:
    first = bugs.add(tmp_path, "Duplicate alert missing", "Steps: scan twice\nExpected: alert\nActual: none")
    second = bugs.add(tmp_path, "Banner shows yesterday", reporter="QA")
    assert (first.id, second.id) == ("BUG-1", "BUG-2")
    text = bugs.path(tmp_path).read_text().replace("## BUG-1 · open", "## BUG-1 · verified")
    bugs.path(tmp_path).write_text(text)
    loaded = bugs.load(tmp_path)
    assert [(b.id, b.status, b.reporter) for b in loaded] == [("BUG-1", "verified", "owner"), ("BUG-2", "open", "QA")]
    assert loaded[0].body.startswith("Severity: major\nSteps: scan twice")
    assert [b.id for b in bugs.unresolved(tmp_path)] == ["BUG-2"]
    assert bugs.add(tmp_path, "third").id == "BUG-3"  # ids are never reused
    with pytest.raises(ValueError):
        bugs.add(tmp_path, "  ")


@pytest.mark.parametrize("summary,blocked", [
    ("📖 reading ~/SourceCode/pantry/api/src/main/java/org/pantry/api/ProductController.java", True),
    ("🔎 finding api/src/**/*.java", True),
    ("▶ running rg -n lookup api/src/main", True),
    ("📖 reading ~/SourceCode/pantry/README.md", False),
    ("📖 reading ~/SourceCode/pantry/.law/PROMPT.md", False),
    ("▶ running curl -sk https://127.0.0.1/api/health", False),
    ("▶ running node ~/LAW/cache/tmp/qa/scan.spec.js", False),
    ("▶ running cat ~/LAW/cache/tmp/qa/run.log", False),
    ("📖 reading ~/SourceCode/pantry/qa/scan.spec.ts", False),  # its own regression pack
    ("▶ running npx playwright test qa/", False),
])
def test_qa_may_not_read_the_product_source(summary: str, blocked: bool) -> None:
    from law.handlers.tasks import reads_product_code

    assert reads_product_code(summary, Path.home() / "SourceCode" / "pantry") is blocked


def _review_rt(tmp_path: Path) -> Any:
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    rt = SimpleNamespace(paths=paths, reg=SimpleNamespace(ports=SimpleNamespace(searxng=8888)))
    rt.roles = SimpleNamespace(get=lambda name: SimpleNamespace(model_role="judge"))
    rt.model_id = lambda role: "m"
    rt.reg.ports.llm_internal = 1
    return rt


async def test_a_qa_bug_fails_the_round_even_when_the_code_review_passes(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "shop"
    (project / "qa").mkdir(parents=True)
    (project / "qa" / "checkout.sh").write_text("true\n")
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        prompts.append(cmd[-1])
        if "--agent" in cmd and cmd[cmd.index("--agent") + 1] == "qa":  # QA finds a bug in the running product
            bugs.add(project, "Checkout button does nothing", "Steps: ... Expected: order ... Actual: nothing", reporter="QA")
            reply = '{"tested": ["checkout: fail"], "summary": "checkout broken"}'
        else:
            reply = '{"score": 5, "issues": [], "summary": "clean code"}'
        return '{"type": "text", "part": {"type": "text", "text": ' + json.dumps(reply) + '}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "build a shop", "dir": str(project), "role": "coder", "project": "shop",
               "rubric_ref": "project.review", "version": 1,
               "spec_args": {"name": "Shop", "out": "docs/proposal", "version": 1},
               "_carry": {"answer": "done"}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    review = done.result["review"]
    assert review["score"] < 4 and review["issues"][0].startswith("BUG-1 (open, major, found by QA)")
    assert review["qa"]["tested"] == ["checkout: fail"] and "QA: checkout broken" in review["summary"]
    qa_prompt = next(p for p in prompts if "independent user" in p)
    assert "regression pack" in qa_prompt and "Retest:" in qa_prompt and ".law/BUGS.md" in qa_prompt


async def test_fix_works_from_a_bug_list_and_follows_the_bugs_protocol(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import tasks

    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        prompts.append(cmd[-1])
        return '{"type": "text", "part": {"type": "text", "text": "fixed"}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "x", "dir": str(tmp_path), "role": "coder", "issues": ["BUG-4 (open, found by owner): y"],
               "_carry": {}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_fix"](_ctx(payload))
    assert done.result["fixed"] is True
    assert "BUG-4" in prompts[0] and "status word in the heading to fixed" in prompts[0] and "QA retests" in prompts[0]


def test_bug_tickets_come_from_the_bug_list(tmp_path: Path) -> None:
    from law.board import Ticket, _bug_tickets

    bugs.add(tmp_path, "Banner wrong")
    project = SimpleNamespace(dir=str(tmp_path))
    tickets: dict[str, Ticket] = {}
    _bug_tickets(project, tickets)  # type: ignore[arg-type]
    t = tickets["BUG-1"]
    assert (t.kind, t.column, t.state, t.epic) == ("bug", "todo", "major · open", "Bugs") and "owner" in t.details


def test_severity_defaults_to_major_and_only_blocker_and_major_block(tmp_path: Path) -> None:
    bugs.add(tmp_path, "Owner: totals wrong")  # owner reports are major unless they say otherwise
    bugs.add(tmp_path, "Typo on the banner", "Severity: minor", reporter="QA")
    bugs.add(tmp_path, "App does not start", "Severity: blocker\nEvidence: ~/LAW/cache/tmp/none.png", reporter="QA")
    loaded = bugs.load(tmp_path)
    assert [b.severity for b in loaded] == ["major", "minor", "blocker"]
    assert [b.id for b in bugs.blocking(tmp_path)] == ["BUG-1", "BUG-3"]
    assert loaded[2].screenshots == []  # named but missing on disk: not attached


async def test_qa_only_run_skips_the_code_review_and_alerts_a_new_blocker(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from law import deliver
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "shop"
    project.mkdir()
    shot = tmp_path / "home.png"
    shot.write_bytes(b"png")
    agents: list[str] = []
    sent: list[tuple[list[Path], str]] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        agents.append(cmd[cmd.index("--agent") + 1])
        bugs.add(project, "Home page is blank", f"Severity: blocker\nEvidence: {shot}", reporter="QA")
        bugs.add(project, "Button text cut off", "Severity: minor", reporter="QA")
        return '{"type": "text", "part": {"type": "text", "text": "{\\"tested\\": [\\"home: fail\\"], \\"summary\\": \\"blank home\\"}"}}'

    def fake_send(env_file: Path, files: list[Path], message: str, transport: Any = None) -> int:
        sent.append((files, message))
        return 1

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(deliver, "to_telegram", fake_send)
    payload = {"prompt": "x", "dir": str(project), "role": "coder", "project": "shop", "qa_only": True,
               "rubric_ref": "project.review", "version": 1, "spec_args": {"name": "Shop", "version": 1},
               "_carry": {}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    review = done.result["review"]
    assert agents == ["qa"]  # no code review in a QA-only run
    assert review["score"] < 4 and review["qa"]["open"][1].startswith("BUG-1 (open, blocker")
    assert any("BUG-2" in i for i in review["issues"]) and not any("BUG-2" in i for i in review["qa"]["open"])
    assert len(sent) == 1 and sent[0][0] == [shot] and "blocker" in sent[0][1] and "Home page is blank" in sent[0][1]


async def test_a_minor_bug_alone_does_not_fail_the_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "shop"
    (project / "qa").mkdir(parents=True)
    (project / "qa" / "labels.sh").write_text("true\n")

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[cmd.index("--agent") + 1] == "qa":
            bugs.add(project, "Label slightly off", "Severity: minor", reporter="QA")
            reply = '{"tested": ["labels: pass"], "summary": "fine"}'
        else:
            reply = '{"score": 5, "issues": [], "summary": "clean"}'
        return '{"type": "text", "part": {"type": "text", "text": ' + json.dumps(reply) + '}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "x", "dir": str(project), "role": "coder", "project": "shop", "version": 1,
               "rubric_ref": "project.review", "spec_args": {"name": "Shop", "out": "docs/proposal", "version": 1},
               "_carry": {}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    assert done.result["review"]["score"] == 5 and done.result["review"]["qa"]["minor"]


async def test_qa_without_evidence_cannot_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-10-29: a QA session stopped mid-sentence, no bugs were on file, and Pantry got 5/5 without any test."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "shop"
    project.mkdir()
    agents: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        agents.append(cmd[cmd.index("--agent") + 1])
        reply = "I am continuing with M1 now." if agents[-1] == "qa" else '{"score": 5, "issues": [], "summary": "ok"}'
        return '{"type": "text", "part": {"type": "text", "text": ' + json.dumps(reply) + '}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "x", "dir": str(project), "role": "coder", "project": "shop", "version": 1,
               "rubric_ref": "project.review", "spec_args": {"name": "Shop", "out": "docs/proposal", "version": 1},
               "_carry": {}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    review = done.result["review"]
    assert agents.count("qa") == 2  # once more before giving up
    assert review["score"] < 4 and "QA evidence missing: no test results" in review["issues"][0]


async def test_a_design_is_checked_by_a_model_that_can_see(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "shop"
    (project / "qa").mkdir(parents=True)
    (project / "qa" / "a.sh").write_text("true\n")
    (project / "docs" / "design" / "reference").mkdir(parents=True)
    (project / "docs" / "design" / "ui-spec.md").write_text("# UI\n")
    (project / "docs" / "design" / "reference" / "look.webp").write_bytes(b"x")
    prompts: dict[str, str] = {}

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        agent = cmd[cmd.index("--agent") + 1]
        prompts[agent] = cmd[-1]
        if agent == "designer":
            bugs.add(project, "No bottom nav", "Severity: major", reporter="QA")
            reply = '{"screens": ["Home: fail"], "summary": "Home is missing"}'
        elif agent == "qa":
            reply = '{"tested": ["add: pass"], "summary": "works"}'
        else:
            reply = '{"score": 5, "issues": [], "summary": "ok"}'
        return '{"type": "text", "part": {"type": "text", "text": ' + json.dumps(reply) + '}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "x", "dir": str(project), "role": "coder", "project": "shop", "version": 1,
               "rubric_ref": "project.review", "spec_args": {"name": "Shop", "out": "docs/proposal", "version": 1},
               "_carry": {}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    assert "docs/design/reference/look.webp" in prompts["designer"] and "390 x 844" in prompts["designer"]
    assert done.result["review"]["score"] < 4 and "No bottom nav" in done.result["review"]["issues"][0]


async def test_a_document_gets_the_editor_and_a_page_check_instead_of_qa(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    book = tmp_path / "RnD" / "bank"
    (book / ".law").mkdir(parents=True)
    agents: list[str] = []
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        prompts.append(cmd[-1])
        agent = cmd[cmd.index("--agent") + 1] if "--agent" in cmd else ""
        agents.append(agent)
        if agent == "editor":
            bugs.add(book, "Ch. 2 says PIDM covers RM 200k; the source says RM 250k", "Severity: blocker",
                     reporter="editor")
            reply = '{"tested": ["ch2: fail"], "summary": "one wrong figure"}'
        elif agent == "designer":
            reply = '{"screens": ["page 14: fail"], "summary": "ledger diagram cut"}'
        else:
            reply = '{"score": 5, "issues": [], "summary": "fine"}'
        return '{"type": "text", "part": {"type": "text", "text": ' + json.dumps(reply) + '}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "write a book", "dir": str(book), "role": "writer", "project": "bank",
               "rubric_ref": "project.review", "version": 1,
               "spec_args": {"name": "Bank", "out": "docs/proposal", "version": 1, "kind": "document",
                             "spec_file": ".law/PROMPT.md (the agreed plan)"},
               "_carry": {"answer": "done"}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    review = done.result["review"]
    assert "qa" not in agents and agents[:2] == ["editor", "designer"]
    assert review["score"] < 4 and review["issues"][0].startswith("BUG-1 (open, blocker, found by editor)")
    assert any("open the cited source" in p for p in prompts) and any("pdftoppm" in p for p in prompts)


CHAPTER = """# Chapter one

Opening words.

## Licensing

BNM issues the licence.[^lic] It caps assets.[^cap]

### A sub-part

More words about licensing.

## Short

Tiny.

## Capital

Capital is RM100 million.[^ch2-cap]

[^lic]: BNM, Licensing Framework, para 5.1, https://www.bnm.gov.my/x
[^cap]: BNM FAQ, Q4, https://www.bnm.gov.my/y
"""


def test_a_document_is_split_into_sections_with_their_footnotes(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import tasks

    monkeypatch.setattr(tasks, "SECTION_WORDS_MIN", 5)
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "01-one.md").write_text(CHAPTER)
    (tmp_path / "chapters" / "02-two.md").write_text("# Two\n\n## Money\n\nIt cites one.[^cap] And more words here.\n")
    secs = tasks.doc_sections(tmp_path)
    assert [s["heading"] for s in secs] == ["Licensing", "Short + Capital", "Money"]
    first, capital, money = secs
    assert first["text"].startswith("# Chapter one") and "### A sub-part" in first["text"]  # the opening goes along
    assert "[^lic]: BNM, Licensing" in first["notes"] and "[^cap]: BNM FAQ" in first["notes"]
    assert "## Short" in capital["text"] and "[^lic]:" not in capital["text"]  # short one merged; no definitions
    assert "[^ch2-cap]: (not defined anywhere)" in capital["notes"]
    assert "[^cap]: BNM FAQ" in money["notes"]  # a definition from another chapter
    assert "02-two.md: ## Money" in tasks.doc_headings(tmp_path)


async def test_a_document_with_chapters_is_fact_checked_one_section_at_a_time(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-11-24: the whole-document check overflowed the editor's context and looped for 70 minutes."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    monkeypatch.setattr(tasks, "SECTION_WORDS_MIN", 5)
    book = tmp_path / "RnD" / "bank"
    (book / ".law").mkdir(parents=True)
    (book / "chapters").mkdir()
    (book / "chapters" / "01-one.md").write_text(CHAPTER)
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        agent = cmd[cmd.index("--agent") + 1] if "--agent" in cmd else ""
        if agent == "editor":
            prompts.append(cmd[-1])
            silent = "section 2 of 2" in cmd[-1]  # this unit never reports, even when asked again
            reply = "" if silent else '{"tested": ["unit: pass"], "summary": "ok"}'
        elif agent == "designer":
            reply = '{"screens": ["page 1: pass"], "summary": "fine"}'
        else:
            reply = '{"score": 5, "issues": [], "summary": "fine"}'
        return '{"type": "text", "part": {"type": "text", "text": ' + json.dumps(reply) + '}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "write a book", "dir": str(book), "role": "writer", "project": "bank",
               "rubric_ref": "project.review", "version": 1,
               "spec_args": {"name": "Bank", "out": "docs/proposal", "version": 1, "kind": "document",
                             "spec_file": ".law/PROMPT.md (the agreed plan)"},
               "_carry": {"answer": "done"}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    assert "Other sessions" in prompts[0] and "01-one.md: ## Capital" in prompts[0]  # the whole-document unit first
    assert "section 1 of 2" in prompts[1] and "It caps assets" in prompts[1] and "[^cap]: BNM FAQ" in prompts[1]
    assert "Capital is RM100 million" not in prompts[1]
    assert len(prompts) == 4  # whole, section 1, section 2 and its one retry
    review = done.result["review"]
    assert review["score"] < 4 and any("section not fact-checked: Short + Capital" in i for i in review["issues"])


async def test_a_review_that_runs_again_goes_on_from_the_next_unit(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-11-25: a review round hit the 6-hour job limit in section 4 of 6 and started over from scratch.
    Each unit also runs with the chapter, RESEARCH.md and sources unreadable whole (they are searched), which keeps
    the judge model's sessions under the size where oMLX stopped reusing its prompt cache."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    monkeypatch.setattr(tasks, "SECTION_WORDS_MIN", 5)
    book = tmp_path / "RnD" / "bank"
    (book / ".law").mkdir(parents=True)
    (book / "chapters").mkdir()
    (book / "chapters" / "01-one.md").write_text(CHAPTER)
    rt = _review_rt(tmp_path)
    first = tasks.doc_sections(book)[0]
    progress = tasks.review_progress_file(rt.paths.cache, "j1", 1)
    tasks.save_review_progress(progress, {"whole": {"tested": ["retest: pass"], "summary": "earlier run"},
                                          tasks.section_key(first): {"tested": ["s1: pass"], "summary": "s1 ok"}})
    prompts: list[str] = []
    limits: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        agent = cmd[cmd.index("--agent") + 1] if "--agent" in cmd else ""
        if agent == "editor":
            prompts.append(cmd[-1])
            limits.append(kw["env"].get("OPENCODE_CONFIG_CONTENT", ""))
            reply = '{"tested": ["unit: pass"], "summary": "ok"}'
        elif agent == "designer":
            reply = '{"screens": ["page 1: pass"], "summary": "fine"}'
        else:
            reply = '{"score": 5, "issues": [], "summary": "fine"}'
        return '{"type": "text", "part": {"type": "text", "text": ' + json.dumps(reply) + '}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    payload = {"prompt": "write a book", "dir": str(book), "role": "writer", "project": "bank",
               "rubric_ref": "project.review", "version": 1,
               "spec_args": {"name": "Bank", "out": "docs/proposal", "version": 1, "kind": "document",
                             "spec_file": ".law/PROMPT.md (the agreed plan)"},
               "_carry": {"answer": "done"}}
    await tasks.make_handlers(rt)["task_review"](_ctx(payload))
    assert len(prompts) == 1 and "section 2 of 2" in prompts[0]  # only the unit not done before
    assert not re.search(r"<YYYY-MM-DD HH:MM>", prompts[0]) and re.search(r"editor · \d{4}-\d\d-\d\d \d\d:\d\d",
                                                                          prompts[0])
    deny = json.loads(limits[0])["agent"]["editor"]["permission"]["read"]
    assert deny["*chapters/*"] == deny["*.law/RESEARCH.md"] == deny["*sources/*"] == "deny" and deny["*"] == "allow"
    assert not progress.exists()  # the round is over: the next round checks everything again


def test_an_edited_section_is_checked_again() -> None:
    from law.handlers import tasks

    sec = {"file": "chapters/01.md", "heading": "Capital", "text": "RM100 million"}
    assert tasks.section_key(sec) != tasks.section_key({**sec, "text": "RM300 million"})
    assert tasks.read_limits("editor", ()) == {}
