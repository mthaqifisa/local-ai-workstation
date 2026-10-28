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
    assert [p[-1].split("This session: ticket ", 1)[1].split(" (", 1)[0] for p in prompts] == [
        "P-2 products", "P-3 batches"]
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
    done = await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert sessions == tasks.IDLE_SESSIONS
    assert "_again" not in done.result  # a stuck stage does not ask for more stages


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


async def test_a_stage_keeps_within_one_job_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-26: a milestone stage ran past the worker's 6 h job limit and used up a retry."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks
    from law.queue.worker import JOB_TIMEOUT_S

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    (project / ".law" / "PLAN.md").write_text(PLAN)
    (project / ".law" / "check.sh").write_text("exit 0\n")
    timeouts: list[float] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] == "opencode":
            timeouts.append(kw["timeout_s"])
            raise TimeoutError  # the session runs out of the stage's time
        return ""

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    done = await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert done.result["dir"] == str(project)  # the stage ends cleanly; the next one continues the ticket
    assert done.result["_again"] is True  # tickets are open: the worker adds a stage if the pipeline has none left
    assert len(timeouts) == 1 and timeouts[0] <= JOB_TIMEOUT_S - tasks.CHECK_TIMEOUT_S
    assert not (project / ".law" / "session.json").exists()

    monkeypatch.setattr(tasks, "STAGE_BUDGET_S", 0)  # budget used: no new ticket starts
    timeouts.clear()
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert timeouts == []


async def test_law_ends_a_session_once_its_ticket_is_ticked_and_committed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-27: OpenCode continued P-22.4's session into P-22.5, so LAW never checked P-22.4."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks
    from law.handlers.base import Steered

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("exit 0\n")
    dirty = ["ui/src/a.ts"]
    monkeypatch.setattr(tasks, "uncommitted", lambda folder: list(dirty))
    actions: list[str | None] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] != "opencode":
            return ""
        steer = kw["steer"]
        _tick(plan, "P-2")
        actions.append(steer("✏️ writing ui/src/a.ts"))  # ticked but not committed yet: keep going
        dirty.clear()
        message = steer("▶ running git commit -m 'P-2: products'")  # committed: LAW ends the session
        actions.append(message)
        raise Steered('{"type": "text", "part": {"type": "text", "text": "P-2 done"}}', message or "")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    done = await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert actions == [None, tasks.TICKET_DONE]
    assert done.result["answer"] == "P-2 done"


async def test_a_nudged_session_keeps_one_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-27: each resume after a nudge got the full session time again, so a stage overran its budget."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks
    from law.handlers.base import Stalled

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("exit 0\n")
    clock = [1000.0]
    monkeypatch.setattr(tasks.time, "monotonic", lambda: clock[0])
    timeouts: list[float] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] != "opencode":
            return ""
        timeouts.append(kw["timeout_s"])
        clock[0] += 1800  # each run takes 30 min
        if len(timeouts) == 1:
            raise Stalled('{"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "x"}}', 1500)
        _tick(plan, "P-2")
        return '{"type": "text", "part": {"type": "text", "text": "done"}}'

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert len(timeouts) == 2 and timeouts[1] == timeouts[0] - 1800  # the resume gets what is left, not a fresh cap


def test_the_rules_ask_for_simple_code_and_small_files() -> None:
    """Owner, 2026-10-27: optimized, simple code, no simple task written in a complex way. Pantry's inventory screen
    had grown to a 624-line component and a 1,008-line test file that every ticket had to re-read."""
    from law.handlers.project import DEV_SPEC
    from law.handlers.tasks import CODE_RUBRIC
    from law.roles import load

    coder = load(REPO / "settings" / "roles.yaml").get("coder").prompt
    assert "simplest code" in coder and "400 lines" in coder
    assert "Split <file>" in DEV_SPEC and "over-engineering" in CODE_RUBRIC and "400 lines" in CODE_RUBRIC


def _git_init(project: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)


async def test_a_ticket_session_gets_the_project_map_and_works_targeted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner, 2026-10-27: work like one Jira story; scan for what exists, read only what the ticket touches."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    (project / "ui").mkdir()
    (project / "ui" / "App.tsx").write_text("a\nb\nc\n")
    (project / "ui" / "package-lock.json").write_text("{}\n")
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("exit 0\n")
    _git_init(project)
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] == "opencode":
            prompts.append(cmd[-1])
            _tick(plan, "P-2")
            return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})
        return ""

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert "Scan, don't read" in prompts[0] and "    3 ui/App.tsx" in prompts[0]
    assert "package-lock.json" not in prompts[0] and ".law/PLAN.md" not in prompts[0].split("Project map")[1]


def test_a_slightly_indented_milestone_is_still_read(tmp_path: Path) -> None:
    """Seen 2026-10-27: an agent indented a new milestone and its tickets by one space; LAW skipped them all."""
    from law import board

    (tmp_path / ".law").mkdir()
    plan = ("## Build version 1\n### M6 E2E\n- [x] P-25 perf\n\n ### M7 UX\n - [ ] P-27 layout fix\n"
            "      Acceptance: no overflow.\n   - [ ] detail of P-27\n - [ ] P-28 no barcode\n")
    (tmp_path / ".law" / "PLAN.md").write_text(plan)
    assert next_ticket(tmp_path, 1) == ("M7 UX", "P-27 layout fix")
    items = board.plan_tickets(plan.split("## Build version 1", 1)[1])
    assert [(t["id"], t["milestone"]) for t in items] == [("P-25", "M6 E2E"), ("P-27", "M7 UX"), ("P-28", "M7 UX")]


def test_a_file_changed_by_a_shell_command_counts_as_work(tmp_path: Path) -> None:
    """Seen 2026-10-28: 25 min of real edits through python scripts were struck as a stall."""
    import os
    import time

    from law.handlers.tasks import changed_since

    (tmp_path / "spec.md").write_text("a\n")
    _git_init(tmp_path)
    (tmp_path / "spec.md").write_text("b\n")  # modified, as a script would
    now = time.time()
    os.utime(tmp_path / "spec.md", (now, now))
    assert changed_since(tmp_path, now - 60)
    assert not changed_since(tmp_path, now + 60)


async def test_reading_the_same_file_again_and_again_is_nudged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-28: P-33 opened the same five files three times in an hour without writing anything."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks
    from law.handlers.base import Steered

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("exit 0\n")
    monkeypatch.setattr(tasks, "uncommitted", lambda folder: [])
    seen: list[str | None] = []
    calls = 0

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        nonlocal calls
        if cmd[0] != "opencode":
            return ""
        calls += 1
        steer = kw["steer"]
        if calls == 1:
            reads = ["📖 reading ~/pantry/.law/PLAN.md"] * 4 + [  # the plan is reread by design
                     "📖 reading ~/pantry/api/Batch.java", "📖 reading ~/pantry/api/Batch.java",
                     "✏️ editing ~/pantry/api/Batch.java",  # an edit resets that file's count
                     "📖 reading ~/pantry/api/Batch.java", "📖 reading ~/pantry/api/Batch.java",
                     "📖 reading ~/pantry/api/Batch.java"]
            for r in reads:
                message = steer(r)
                seen.append(message)
                if message:
                    raise Steered('{"type": "text", "sessionID": "ses_9", "part": {"type": "text", "text": "x"}}',
                                  message)
        _tick(plan, "P-2")
        return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert seen[:9] == [None] * 9
    assert seen[9] is not None and "Batch.java 3 times" in seen[9]


async def test_a_ticket_stalled_twice_is_told_to_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-28: P-22 and P-33 planned the whole ticket for hours; splitting P-22 by hand unblocked it."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks
    from law.handlers.base import Stalled

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("exit 0\n")
    monkeypatch.setattr(tasks, "uncommitted", lambda folder: [])
    monkeypatch.setattr(tasks, "changed_since", lambda folder, since, **kw: False)
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] != "opencode":
            return ""
        prompts.append(cmd[-1])
        if len(prompts) <= 2:
            raise Stalled('{"type": "text", "sessionID": "ses_s", "part": {"type": "text", "text": "x"}}', 1500)
        _tick(plan, "P-2")
        return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert "build or tests" in prompts[1] and "too big to start" in prompts[2]


def test_split_tickets_count_at_any_indent(tmp_path: Path) -> None:
    """Seen 2026-10-28: the agent split P-37 and indented P-37.2 to P-37.5 by two spaces; they read as details."""
    (tmp_path / ".law").mkdir()
    plan = tmp_path / ".law" / "PLAN.md"
    plan.write_text("## Build version 1\n### M7 Lookup\n - [x] P-37.1 interface\n  - [ ] P-37.2 sister databases\n"
                    "    - [ ] wire the chain\n- [ ] P-34 usage UI\n")
    assert next_ticket(tmp_path, 1) == ("M7 Lookup", "P-37.2 sister databases")
    _tick(plan, "P-37.2")
    assert next_ticket(tmp_path, 1) == ("M7 Lookup", "P-34 usage UI")


async def test_editing_the_plan_does_not_reset_a_ticket_sessions_stall_count(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-28: after a stall nudge a P-37.1 session rewrote its to-do list in PLAN.md and read on, an hour
    without code; the plan edit counted as progress, so the split nudge never came."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks
    from law.handlers.base import Stalled

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("exit 0\n")
    monkeypatch.setattr(tasks, "uncommitted", lambda folder: [])
    monkeypatch.setattr(tasks, "changed_since", lambda folder, since, **kw: False)
    plan_edit = json.dumps({"type": "tool_use", "sessionID": "ses_s", "part": {
        "type": "tool", "tool": "edit", "state": {"status": "completed", "input": {"filePath": str(plan)}}}})
    prompts: list[str] = []
    seen: list[Any] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] != "opencode":
            return ""
        prompts.append(cmd[-1])
        seen.append(kw["is_work"])
        if len(prompts) <= 2:
            raise Stalled(plan_edit, 1500)
        _tick(plan, "P-2")
        return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert "too big to start" in prompts[2]
    assert not seen[0](plan_edit)  # nor does it hold off the stall guard
    code_edit = plan_edit.replace(str(plan), str(project / "api" / "Lookup.java"))
    assert seen[0](code_edit)


def test_changed_since_can_skip_law_files(tmp_path: Path) -> None:
    import subprocess
    import time

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".law").mkdir()
    before = time.time() - 1
    (tmp_path / ".law" / "PLAN.md").write_text("plan\n")
    assert tasks_changed_since(tmp_path, before) and not tasks_changed_since(tmp_path, before, code_only=True)
    (tmp_path / "Lookup.java").write_text("class Lookup {}\n")
    assert tasks_changed_since(tmp_path, before, code_only=True)


def tasks_changed_since(project: Path, since: float, **kw: Any) -> bool:
    from law.handlers.tasks import changed_since

    return changed_since(project, since, **kw)


def test_a_split_ticket_gives_way_to_its_sub_tickets(tmp_path: Path) -> None:
    """Seen 2026-10-28: P-34 was split into P-34.1 to P-34.6 below its own unticked line."""
    (tmp_path / ".law").mkdir()
    plan = tmp_path / ".law" / "PLAN.md"
    plan.write_text("## Build version 1\n### M7 Usage\n - [ ] P-34 usage UI\n   details\n  - [ ] P-34.1 stock rows\n"
                    "  - [ ] P-34.2 history\n- [ ] P-35 next\n")
    assert next_ticket(tmp_path, 1) == ("M7 Usage", "P-34.1 stock rows")
    _tick(plan, "P-34.1")
    _tick(plan, "P-34.2")
    assert next_ticket(tmp_path, 1) == ("M7 Usage", "P-34 usage UI")  # ticked once its parts are


def test_probe_files_are_recognised() -> None:
    from law.handlers.tasks import PROBE_FILE

    for seen in ["~/p/ui/src/views/scratch-dump.test.tsx", "~/p/ui/src/views/probe-p345.test.tsx",
                 "~/p/ui/src/scratch.probe.test.tsx", "~/p/api/src/test/java/x/ScratchDebugTests.java",
                 "~/p/api/src/test/java/x/ScratchLowerByteaProbe.java", "~/p/e2e/debug.spec.ts"]:
        assert PROBE_FILE.search(seen), seen
    for real in ["~/p/ui/src/views/UseSome.test.tsx", "~/p/ui/src/lib/debugLog.ts", "~/p/api/src/x/DumpService.java",
                 "~/p/ui/src/views/InventoryView.tsx"]:
        assert not PROBE_FILE.search(real), real


async def test_a_second_probe_file_gets_a_time_box_without_a_strike(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seen 2026-10-26 to -28: the stuck episodes all wrote probe files (five in P-22, two in P-34.5)."""
    from law.handlers import project as _registers_project_specs  # noqa: F401
    from law.handlers import tasks
    from law.handlers.base import Steered

    project = tmp_path / "pantry"
    (project / ".law").mkdir(parents=True)
    plan = project / ".law" / "PLAN.md"
    plan.write_text(PLAN.replace("- [ ] P-3 batches\n", ""))
    (project / ".law" / "check.sh").write_text("exit 0\n")
    monkeypatch.setattr(tasks, "uncommitted", lambda folder: [])
    seen: list[str | None] = []
    prompts: list[str] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        if cmd[0] != "opencode":
            return ""
        prompts.append(cmd[-1])
        if len(prompts) == 1:
            for line in ["✏️ writing ~/pantry/ui/src/probe-a.test.tsx", "✏️ writing ~/pantry/ui/src/probe-a.test.tsx"]:
                message = kw["steer"](line)
                seen.append(message)
                if message:
                    raise Steered('{"type": "text", "sessionID": "ses_p", "part": {"type": "text", "text": "x"}}',
                                  message)
        _tick(plan, "P-2")
        return json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await tasks.make_handlers(_rt(tmp_path))["task_work"](_ctx(_payload(project)))
    assert seen[0] is None and seen[1] == tasks.PROBE_NUDGE
    assert prompts[1] == tasks.PROBE_NUDGE  # sent into the same session
