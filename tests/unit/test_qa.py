"""Black-box QA (ADR-0018): the bug list, the QA round inside a project review, `/bugfix` and the board's bug tickets."""

from __future__ import annotations

import asyncio
import json
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
    assert loaded[0].body.startswith("Steps: scan twice")
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
    project.mkdir()
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
               "rubric_ref": "project.review", "spec_args": {"name": "Shop", "out": "docs/proposal"},
               "_carry": {"answer": "done"}}
    done = await tasks.make_handlers(_review_rt(tmp_path))["task_review"](_ctx(payload))
    review = done.result["review"]
    assert review["score"] < 4 and review["issues"][0].startswith("BUG-1 (open, found by QA)")
    assert review["qa"]["tested"] == ["checkout: fail"] and "QA: checkout broken" in review["summary"]
    qa_prompt = next(p for p in prompts if "independent user" in p)
    assert "Retest first" in qa_prompt and ".law/BUGS.md" in qa_prompt


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
    assert (t.kind, t.column, t.state, t.epic) == ("bug", "todo", "open", "Bugs") and "owner" in t.details
