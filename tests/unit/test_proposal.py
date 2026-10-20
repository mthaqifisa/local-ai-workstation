"""/proposal pipeline: stage plan, diagram extraction, rendering and mailing (external tools faked)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from law import mailer, secrets, specs
from law.handlers import proposal
from law.paths import LawPaths
from law.queue.worker import Done, Stages

REPO = Path(__file__).resolve().parents[2]
DOC = """---
title: LAN Chat
---

# Executive summary

Build it. **Recommendation:** proceed.

# Architecture

```{.mermaid caption="Context diagram"}
flowchart LR
  A --> B
```

```mermaid
sequenceDiagram
  A->>B: hi
```

# Risks

None.
"""


def _rt(tmp_path: Path, email: str | None = "me@example.com") -> Any:
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    if email:
        secrets.write(paths.env_file, {"LAW_REPORT_EMAIL": email})
    return SimpleNamespace(paths=paths, reg=SimpleNamespace(ports=SimpleNamespace(searxng=8888)))


def _ctx(payload: dict[str, Any]) -> Any:
    async def progress(_: str) -> None:
        return None

    return SimpleNamespace(job=SimpleNamespace(payload=payload, id="j1", parent="root"), progress=progress,
                           cancel_event=asyncio.Event())


def test_diagrams_become_images_with_captions():
    text, diagrams = proposal.extract_diagrams(DOC)
    assert [c for c, _ in diagrams] == ["Context diagram", "Diagram 2"]
    assert "flowchart LR" in diagrams[0][1] and "```" not in text
    assert "![Context diagram](assets/diagram-1.png){width=100%}" in text


def test_executive_summary_is_the_first_section():
    assert proposal.executive_summary(DOC) == "Build it. **Recommendation:** proceed."
    assert proposal.slug("Propose a LAN chat product for offices!") == "propose-a-lan-chat-product-for-offices"


def test_pipeline_for_a_prototype_project(tmp_path: Path):
    rt = _rt(tmp_path)
    project = tmp_path / "lan-chat"
    project.mkdir()
    out = asyncio.run(proposal.make_handlers(rt)["proposal"](_ctx({"prompt": "Propose it", "dir": str(project)})))
    assert isinstance(out, Stages)
    kinds = [(s["kind"], s["payload"].get("role")) for s in out.specs]
    sections = [s for s in out.specs if s["payload"].get("spec_ref") == "proposal.section"]
    assert kinds == [("task_translate", "writer"), ("task_work", "researcher")] + [("task_work", "writer")] * 16 + [
        ("proposal_check", None), ("task_review", "writer"), ("task_fix", "writer"), ("proposal_render", None),
        ("proposal_mail", None)]
    assert len(sections) == 16  # /proposal is always the full document: one writer session per section
    research = out.specs[1]["payload"]
    assert "http://127.0.0.1:8888/search" in specs.resolve(research) and "docs/proposal/research.md" in specs.resolve(research)
    first, second = specs.resolve(sections[0]["payload"]), specs.resolve(sections[1]["payload"])
    assert "section 1 of 16" in first and "YAML header" in first and '"Executive summary"' in first
    assert "Append the section" in second and "Situation, complication, resolution" in second
    review = out.specs[-4]["payload"]
    assert "partner" in specs.resolve(review, "rubric") and "Fix only what the issues name" in specs.resolve(review)
    assert (project / "docs" / "proposal" / "assets").is_dir()


def test_pipeline_without_a_project_writes_to_outputs(tmp_path: Path):
    rt = _rt(tmp_path)
    out = asyncio.run(proposal.make_handlers(rt)["proposal"](_ctx({"prompt": "A budgeting app for students"})))
    workdir = Path(out.specs[0]["payload"]["dir"])
    assert workdir.parent == rt.paths.outputs / "docs" and workdir.name.endswith("a-budgeting-app-for-students")


def test_missing_report_email_fails_before_any_work(tmp_path: Path):
    rt = _rt(tmp_path, email=None)
    with pytest.raises(mailer.MailError, match="no report email"):
        asyncio.run(proposal.make_handlers(rt)["proposal"](_ctx({"prompt": "x"})))


def test_render_draws_diagrams_and_builds_the_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _rt(tmp_path)
    docs = tmp_path / "p" / "docs" / "proposal"
    (docs / "assets").mkdir(parents=True)
    (docs / "proposal.md").write_text(DOC)
    calls: list[list[str]] = []

    tool = tmp_path / "bin" / "mmdc"  # fake mermaid-cli: draws diagram 1, cannot draw diagram 2
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\ncase \"$*\" in *diagram-2*) echo 'Error: Parse error on line 2:'; exit 1;; esac\n"
                    "for a; do [ \"$prev\" = -o ] && : > \"$a\"; prev=$a; done\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        calls.append(cmd)
        target = Path(cmd[cmd.index("-o") + 1])
        target.write_bytes(b"x" * (20_000 if target.suffix == ".pdf" else 10))  # noqa: ASYNC240
        if cmd[0] == "pandoc":
            calls.append(["rendered", Path(cmd[1]).read_text()])  # noqa: ASYNC240
        return ""

    monkeypatch.setattr(proposal, "run_cmd", fake_run_cmd)
    payload = {"dir": str(tmp_path / "p"), "out": "docs/proposal", "prompt": "x"}
    done = asyncio.run(proposal.make_handlers(rt)["proposal_render"](_ctx(payload)))
    assert isinstance(done, Done) and done.result["pdf"] == str(docs / "proposal.pdf")
    assert done.result["diagrams_failed"] == ["Diagram 2"]
    rendered = next(c[1] for c in calls if c[0] == "rendered")
    assert "assets/diagram-1.png" in rendered and "sequenceDiagram" in rendered
    pandoc = next(c for c in calls if c[0] == "pandoc")
    assert "--pdf-engine=typst" in pandoc and any(a.endswith("templates/docs/proposal.typst") for a in pandoc)
    assert not (docs / ".proposal.render.md").exists()


def test_mail_sends_the_pdf_with_the_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _rt(tmp_path)
    docs = tmp_path / "p"
    docs.mkdir()
    (docs / "proposal.md").write_text(DOC)
    pdf = docs / "proposal.pdf"
    pdf.write_bytes(b"%PDF")
    sent: dict[str, Any] = {}
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, files, work: sent.update(
        to=to, subject=subject, body=body, files=files))
    carry = {"pdf": str(pdf), "review": {"score": 5, "summary": "board-ready"}, "fixed": False}
    done = asyncio.run(proposal.make_handlers(rt)["proposal_mail"](
        _ctx({"dir": str(docs), "out": ".", "prompt": "Propose it", "_carry": carry})))
    assert isinstance(done, Done) and done.result["emailed"] == "me@example.com"
    assert sent["subject"] == "Proposal: LAN Chat" and sent["files"] == [pdf]
    assert "Recommendation:** proceed" in sent["body"] and "5/5" in sent["body"]


def test_mail_failure_does_not_stop_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _rt(tmp_path)
    (tmp_path / "proposal.md").write_text(DOC)
    pdf = tmp_path / "proposal.pdf"
    pdf.write_bytes(b"%PDF")

    def boom(*a: Any) -> None:
        raise mailer.MailError("Mail did not respond")

    monkeypatch.setattr(mailer, "send", boom)
    done = asyncio.run(proposal.make_handlers(rt)["proposal_mail"](
        _ctx({"dir": str(tmp_path), "out": ".", "prompt": "x", "_carry": {"pdf": str(pdf)}})))
    assert isinstance(done, Done) and done.result["emailed"] is None and done.result["pdf"] == str(pdf)
    assert "Email not sent: Mail did not respond" in done.result["answer"]


def test_translator_sees_the_fixed_proposal_structure(tmp_path: Path):
    rt = _rt(tmp_path)
    out = asyncio.run(proposal.make_handlers(rt)["proposal"](_ctx({"prompt": "Propose it"})))
    translate = out.specs[0]["payload"]
    assert out.specs[0]["kind"] == "task_translate" and "proposal.md" in specs.resolve(translate)


async def test_translator_prompt_includes_fixed_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from law.handlers import tasks

    seen: list[str] = []

    async def fake_answer(rt_: Any, role: str, messages: list[dict[str, Any]], ctx: Any = None) -> str:
        seen.append(messages[-1]["content"])
        return "## Goal\nx"

    monkeypatch.setattr(tasks, "answer", fake_answer)
    rt = _rt(tmp_path)
    rt.roles = SimpleNamespace(get=lambda name: None)
    handler = tasks.make_handlers(rt)["task_translate"]
    await handler(_ctx({"prompt": "Propose it", "role": "writer", "dir": str(tmp_path), "spec": "WRITE proposal.md"}))
    assert "must not\ncontradict them" in seen[0] and "WRITE proposal.md" in seen[0]


def test_steps_use_the_rules_current_when_they_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _rt(tmp_path)
    out = asyncio.run(proposal.make_handlers(rt)["proposal"](_ctx({"prompt": "Propose it"})))
    writer = next(s["payload"] for s in out.specs if s["payload"].get("role") == "writer"
                  and s["kind"] == "task_work")
    assert "spec" not in writer and writer["spec_ref"] == "proposal.section"  # no frozen text in the pipeline
    monkeypatch.setattr(proposal, "SECTION_SPEC", "NEW RULES for {heading}")  # rules change after creation
    assert specs.resolve(writer) == "NEW RULES for Executive summary"
    assert specs.resolve({"spec": "old text"}) == "old text"  # pipelines created before keep working


def test_brief_proposals_have_fewer_sections():
    full = proposal.write_spec({"out": "docs/proposal", "version": 1})
    brief = proposal.write_spec({"out": "docs/proposal", "version": 1, "depth": "brief"})
    assert "15. Next steps" in full and "15." not in brief and "concise proposal brief" in brief
    assert "eight searches" in proposal.research_spec({"out": ".", "searx": "x", "depth": "brief"})


async def test_a_section_already_written_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from law.handlers import tasks

    rt = _rt(tmp_path)
    project = tmp_path / "p"
    (project / "docs" / "proposal").mkdir(parents=True)
    (project / "docs" / "proposal" / "proposal.md").write_text("---\ntitle: X\n---\n\n# 1. Executive summary\nDone.\n")
    out = await proposal.make_handlers(rt)["proposal"](_ctx({"prompt": "Propose it", "dir": str(project)}))
    sections = [s["payload"] for s in out.specs if s["payload"].get("spec_ref") == "proposal.section"]
    ran: list[str] = []

    async def fake_agent_run(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        ran.append(cmd[-1])
        return ""

    monkeypatch.setattr(tasks, "run_cmd", fake_agent_run)
    rt.roles = SimpleNamespace(get=lambda name: SimpleNamespace(model_role="writer"))
    rt.model_id = lambda role: "m"
    rt.reg.ports.llm_internal = 1
    handlers = tasks.make_handlers(rt)
    await handlers["task_work"](_ctx({**sections[0], "_carry": {}}))
    assert ran == []  # the executive summary is already in the file
    await handlers["task_work"](_ctx({**sections[1], "_carry": {}}))
    assert len(ran) == 1 and "Situation, complication, resolution" in ran[0]


def test_revisions_revise_each_section_and_add_what_changed():
    stages = proposal.section_stages({"dir": "/p", "prompt": "x"}, {"out": "docs/proposal", "depth": "brief",
                                                                     "version": 2, "feedback": "make it mobile-first"}, 2)
    headings = [s["payload"]["spec_args"]["heading"] for s in stages]
    assert headings[:2] == ["Executive summary", "What changed in version 2"] and len(headings) == 8
    text = specs.resolve(stages[2]["payload"])
    assert "Revise this section in place" in text and "make it mobile-first" in text
    assert not stages[0]["payload"]["section"]["skip_if_written"]  # revisions always run


def test_mermaid_errors_name_the_line_and_the_problem():
    out = ("Generating single mermaid chart\n\nError: Parse error on line 3:\n...D1[\"Phone\"], D2[\"Tablet\"]\n"
           "-----------------------^\nExpecting 'SEMI', 'NEWLINE', got 'COMMA'\nParser.parseError (https://x)\n"
           "    at Parser.parse (https://y)\n" + "    at z\n" * 40)
    msg = proposal.mermaid_error(out)
    assert "Parse error on line 3" in msg and "got 'COMMA'" in msg and "Parser.parseError" not in msg
    assert proposal.mermaid_error("nothing useful") == "Mermaid could not draw it"


async def test_undrawable_diagrams_reach_the_review_as_issues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _rt(tmp_path)
    docs = tmp_path / "p" / "docs" / "proposal"
    (docs / "assets").mkdir(parents=True)
    (docs / "proposal.md").write_text(DOC)
    tool = tmp_path / "mmdc"  # a fake mermaid-cli that fails on the second diagram
    tool.write_text("#!/bin/sh\ncase \"$*\" in *diagram-2*) echo 'Error: Parse error on line 3:'; "
                    "echo \"Expecting 'SEMI', got 'COMMA'\"; exit 1;; esac\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    done = await proposal.make_handlers(rt)["proposal_check"](
        _ctx({"dir": str(tmp_path / "p"), "out": "docs/proposal", "prompt": "x"}))
    errors = done.result["diagram_errors"]
    assert len(errors) == 1 and "Diagram 2" in errors[0] and "Parse error on line 3" in errors[0]
    assert "got 'COMMA'" in errors[0]


async def test_review_cannot_pass_with_undrawable_diagrams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from law.handlers import tasks

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        return '{"type": "text", "part": {"type": "text", "text": "{\\"score\\": 5, \\"issues\\": [], \\"summary\\": \\"great\\"}"}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    rt = _rt(tmp_path)
    rt.roles = SimpleNamespace(get=lambda name: SimpleNamespace(model_role="judge"))
    rt.model_id = lambda role: "m"
    rt.reg.ports.llm_internal = 1
    carry = {"answer": "done", "diagram_errors": ["Diagram \"Context\" does not draw: Parse error"]}
    done = await tasks.make_handlers(rt)["task_review"](
        _ctx({"prompt": "x", "dir": str(tmp_path), "role": "writer", "_carry": carry}))
    assert done.result["review"]["score"] == 3 and "does not draw" in done.result["review"]["issues"][0]
