"""Development one ticket per fresh agent session, checked by LAW's own run of `.law/check.sh` (ADR-0019)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from law.handlers.tasks import next_ticket
from law.paths import LawPaths
from law.queue.worker import Fatal

REPO = Path(__file__).resolve().parents[2]
PLAN = ("# Plan\n\n## Build version 1\n### M1 API\n- [x] P-1 skeleton\n- [ ] P-2 products\n  - [ ] detail\n"
        "- [ ] P-3 batches\n### M2 UI\n- [ ] P-4 scan screen\n")


def _ctx(payload: dict[str, Any]) -> Any:
    async def progress(_: str) -> None:
        return None

    return SimpleNamespace(job=SimpleNamespace(payload=payload, id="j1", parent="root"), progress=progress,
                           cancel_event=asyncio.Event())


def _rt(tmp_path: Path) -> Any:
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    rt = SimpleNamespace(paths=paths, reg=SimpleNamespace(ports=SimpleNamespace(searxng=8888, llm_internal=1)))
    rt.roles = SimpleNamespace(get=lambda name: SimpleNamespace(model_role="coder"))
    rt.model_id = lambda role: "m"
    return rt


def _payload(project: Path) -> dict[str, Any]:
    return {"prompt": "build pantry", "dir": str(project), "role": "coder", "milestone": True, "version": 1,
            "project": "pantry", "spec_ref": "project.dev",
            "spec_args": {"name": "Pantry", "out": "docs/proposal", "version": 1, "key": "P"}}


def _tick(plan: Path, ticket: str) -> None:
    plan.write_text(plan.read_text().replace(f"- [ ] {ticket} ", f"- [x] {ticket} "))


def test_next_ticket_is_the_first_top_level_unticked_one(tmp_path: Path) -> None:
    (tmp_path / ".law").mkdir()
    (tmp_path / ".law" / "PLAN.md").write_text(PLAN)
    assert next_ticket(tmp_path, 1) == ("M1 API", "P-2 products")
    assert next_ticket(tmp_path, 2) is None


async def test_each_ticket_gets_a_fresh_session_and_the_stage_ends_with_its_milestone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN)
    (project / ".law" / "check.sh").write_text("exit 0\n")
    prompts: list[list[str]] = []
    checks = 0

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        nonlocal checks
        if cmd[0] == "opencode":
            prompts.append(cmd)
            ticket = cmd[-1].rsplit("This session: ticket ", 1)[1].split(" ", 1)[0]
            _tick(plan, ticket)
            return '{"type": "text", "sessionID": "ses_' + ticket + '", "part": {"type": "text", "text": "done"}}'
        if cmd == ["bash", ".law/check.sh"]:
            checks += 1
            return "BUILD SUCCESS"
        return ""  # git status: everything committed

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    done = await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert done.result["dir"] == str(project)
    assert [p[-1].rsplit("ticket ", 1)[1].split(" (", 1)[0] for p in prompts] == ["P-2 products", "P-3 batches"]
    assert all("--session" not in p for p in prompts)  # fresh each time: the finished session is not resumed
    assert checks == 2 and "- [ ] P-4" in plan.read_text()  # M2 is the next stage's
    assert not (project / ".law" / "session.json").exists()


async def test_a_failing_gate_goes_to_the_next_session_first(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("mvn verify\n")
    prompts: list[str] = []
    gate = iter([Fatal("bash exited 1: [ERROR] ProductControllerTest.rejectsBlank FAILED"), "BUILD SUCCESS"])

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] == "opencode":
            prompts.append(cmd[-1])
            _tick(plan, "P-2")
            return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})
        if cmd[0] == "bash":
            result = next(gate)
            if isinstance(result, Exception):
                raise result
            return result
        return ""

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert len(prompts) == 2  # the ticket, then the fix of what LAW's gate found
    assert "LAW checked the last ticket" in prompts[1] and "rejectsBlank FAILED" in prompts[1]
    assert "This session: ticket P-4 scan screen" in prompts[1]  # the fix comes first, then the next ticket


@pytest.mark.parametrize("script,status,expected", [
    (False, "", ".law/check.sh does not exist"),
    (True, " M api/src/Main.java\n M .law/PROMPT.md\n?? ui/src/Dialog.tsx\n", "api/src/Main.java, ui/src/Dialog.tsx"),
    (True, " M .law/PROGRESS.md\n", ""),
])
async def test_the_gate_wants_the_script_and_nothing_left_uncommitted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: bool, status: str, expected: str) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text("## Build version 1\n### M1 API\n- [ ] P-1 skeleton\n")
    if script:
        (project / ".law" / "check.sh").write_text("exit 0\n")
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] == "opencode":
            prompts.append(cmd[-1])
            _tick(plan, "P-1")
            return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})
        return status if cmd[0] == "git" else "ok"

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    if expected:
        assert len(prompts) >= 2 and expected in prompts[1] and "Every ticket is ticked" in prompts[1]
    else:
        assert len(prompts) == 1


async def test_sessions_that_change_nothing_end_the_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    (project / ".law" / "PLAN.md").write_text(PLAN)
    (project / ".law" / "check.sh").write_text("exit 0\n")
    sessions = 0

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        nonlocal sessions
        sessions += cmd[0] == "opencode"
        return json.dumps({"type": "text", "part": {"type": "text", "text": "nothing"}}) if cmd[0] == "opencode" else ""

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert sessions == tasks.IDLE_SESSIONS


async def test_a_new_build_writes_the_plan_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    (project / ".law" / "check.sh").write_text("exit 0\n")
    plan = project / ".law" / "PLAN.md"
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] == "opencode":
            prompts.append(cmd[-1])
            if len(prompts) == 1:
                plan.write_text("## Build version 1\n### M1 API\n- [x] P-1 skeleton\n- [ ] P-2 products\n")
            else:
                _tick(plan, "P-2")
            return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})
        return ""

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert "has no build section" in prompts[0] and "ticket P-2 products (milestone M1 API)" in prompts[1]
    assert len(prompts) == 2


def test_the_board_reads_split_ticket_ids() -> None:
    from law import board

    items = board.plan_tickets("### M5 UI\n- [x] P-22.1 select batches\n- [ ] P-22.2 delete selected\n")
    assert [(t["id"], t["done"]) for t in items] == [("P-22.1", True), ("P-22.2", False)]


def test_missing_gate_tooling_becomes_its_own_ticket() -> None:
    """Seen 2026-10-26: a frontend ticket's session spent a 7.6K-token reply weighing whether to add Spotless,
    Checkstyle and JaCoCo (missing from the project) inside that ticket."""
    from law.handlers.project import DEV_SPEC

    assert "do not add it inside the current ticket" in DEV_SPEC and "Add the missing gate tooling" in DEV_SPEC
