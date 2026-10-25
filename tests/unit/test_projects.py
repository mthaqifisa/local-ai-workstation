"""Project lifecycle: template, state machine, phases and gates (agents and git faked where needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from law import projects, secrets, specs
from law.handlers import project as project_handlers
from law.handlers.tasks import agent_env, plan_complete, project_env
from law.paths import LawPaths
from law.queue.worker import Done, Stages

REPO = Path(__file__).resolve().parents[2]
FILLED = """/code new
name: Office Chat
repo:
repo_url:
stack: Spring Boot + Angular
thinking: high
summary: Chat for everyone on the office Wi-Fi
details:
One username per device.
Modern UI.
"""


def test_template_round_trip():
    fields = projects.parse_template(FILLED)
    assert fields["proposal"] == "brief"  # the default depth
    assert projects.parse_template(FILLED.replace("thinking: high", "thinking: high\nproposal: full"))[
        "proposal"] == "full"
    assert fields["repo"] == "office-chat" and fields["thinking"] == "high"
    assert fields["details"] == "One username per device.\nModern UI."
    assert projects.parse_template(projects.TEMPLATE.replace("name:", "name: X").replace(
        "summary:", "summary: s").replace("details:", "details: d"))["repo"] == "x"


@pytest.mark.parametrize("change,error", [
    (("name: Office Chat", "name:"), "name"),
    (("thinking: high", "thinking: max"), "low, medium or high"),
    (("repo:", "repo: Bad Name"), "folder name"),
    (("repo_url:", "repo_url: https://user:secret@github.com/me/x.git"), "without a password"),
])
def test_template_errors_say_what_is_wrong(change, error):
    with pytest.raises(projects.TemplateError, match=error):
        projects.parse_template(FILLED.replace(*change))


def test_repo_urls():
    for url in ("https://github.com/me/office-chat.git", "git@github.com:me/office-chat.git"):
        assert projects.parse_template(FILLED.replace("repo_url:", f"repo_url: {url}"))["repo_url"] == url


def _project(**kw: Any) -> projects.Project:
    base = dict(id="office-chat", name="Office Chat", dir="/x", summary="s", details="d")
    return projects.Project(**{**base, **kw})


def test_state_machine():
    p = _project(state="awaiting_go")
    assert projects.decide(p, "go") == "develop" and p.state == "developing"
    with pytest.raises(ValueError, match="will ask for your decision when it is ready"):
        projects.decide(p, "go")
    p.state = "awaiting_verification"
    assert projects.decide(p, "changes", "add file sharing") == "proposal"
    assert (p.state, p.version, p.feedback) == ("proposing", 2, "add file sharing")
    p.state = "awaiting_go"
    assert projects.decide(p, "nogo", "") == "" and p.state == "parked"
    assert projects.decide(p, "resume", "smaller scope") == "proposal" and p.version == 3
    p.state = "awaiting_verification"
    assert projects.decide(p, "allgood") == "" and p.state == "done"
    with pytest.raises(ValueError, match="no repo_url"):
        projects.decide(p, "push")
    with pytest.raises(ValueError, match="say what should change"):
        projects.decide(_project(state="awaiting_verification"), "changes", " ")
    assert [e.event for e in p.history] == ["go", "changes", "nogo", "resume", "allgood"]


def test_store_finds_projects_by_prefix(tmp_path: Path):
    store = projects.Projects(LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path))
    p = store.create(projects.parse_template(FILLED), chat=7)
    store.save(p)
    assert store.get("office").name == "Office Chat" and store.get("office-chat").chat == 7
    assert (Path(p.dir) / ".law" / "project.json").exists()
    with pytest.raises(projects.TemplateError, match="already exists"):
        store.create(projects.parse_template(FILLED), chat=7)
    with pytest.raises(KeyError, match="office-chat"):
        store.get("nope")


def _rt(tmp_path: Path) -> Any:
    paths = LawPaths(repo=REPO, home=tmp_path / "LAW", user_home=tmp_path)
    paths.projects.mkdir(parents=True, exist_ok=True)
    secrets.write(paths.env_file, {"LAW_REPORT_EMAIL": "me@example.com"})
    return SimpleNamespace(paths=paths, reg=SimpleNamespace(ports=SimpleNamespace(searxng=8888)))


def _ctx(payload: dict[str, Any]) -> Any:
    async def progress(_: str) -> None:
        return None

    return SimpleNamespace(job=SimpleNamespace(payload=payload, id="j", parent=None), progress=progress,
                           cancel_event=asyncio.Event())


def _run(rt: Any, kind: str, payload: dict[str, Any]) -> Any:
    return asyncio.run(project_handlers.make_handlers(rt)[kind](_ctx(payload)))


def test_new_project_makes_a_repo_and_runs_the_proposal_to_the_gate(tmp_path: Path):
    rt = _rt(tmp_path)
    out = _run(rt, "project_new", {"fields": projects.parse_template(FILLED), "chat": 7})
    assert isinstance(out, Stages)
    kinds = [s["kind"] for s in out.specs]
    assert kinds[0] == "task_translate" and "task_work" in kinds and kinds[-2:] == ["proposal_mail", "project_gate"]
    folder = rt.paths.projects / "office-chat"
    assert (folder / ".git").is_dir() and (folder / "docs" / "proposal" / "assets").is_dir()
    # documents don't use the project's thinking level (that is for its code): sections are written with thinking
    # off, research at the researcher's own level
    work = [s["payload"] for s in out.specs if s["kind"] == "task_work"]
    assert all(w.get("effort") == "off" for w in work if w.get("section"))
    assert all("effort" not in w for w in work if not w.get("section"))
    assert all(s["payload"].get("spec_args", {}).get("depth") in (None, "brief") for s in out.specs)


def test_go_runs_environment_milestones_reviews_then_the_verify_gate(tmp_path: Path):
    rt = _rt(tmp_path)
    store = projects.Projects(rt.paths)
    p = store.create(projects.parse_template(FILLED), chat=7)
    p.state = "awaiting_go"
    store.save(p)
    out = _run(rt, "project_step", {"project": "office", "action": "go", "chat": 7})
    stages = out.specs
    assert "prepares the Mac" in specs.resolve(stages[0]["payload"]) and stages[0]["payload"]["stall_min"] == 45
    assert stages[0]["payload"]["effort"] == "xhigh"  # the project's thinking level drives development
    milestones = [s for s in stages if s["payload"].get("milestone")]
    assert len(milestones) == project_handlers.MILESTONE_STAGES
    assert [s["kind"] for s in stages[-10:]] == ["task_review", "task_fix"] * 3 + [
        "task_work", "proposal_render", "proposal_mail", "project_gate"]
    assert "proposal.md (version 1)" in specs.resolve(stages[-5]["payload"], "rubric")
    handover = specs.resolve(stages[-4]["payload"])
    assert "SYSTEM HANDOVER" in handover and "screenshots" in handover and "docs/handover/handover.md" in handover
    assert stages[-3]["payload"]["doc"] == "handover" and stages[-2]["payload"]["out"] == "docs/handover"
    assert store.get("office-chat").state == "developing"


def test_a_decision_at_the_wrong_moment_is_refused_as_information(tmp_path: Path):
    rt = _rt(tmp_path)
    store = projects.Projects(rt.paths)
    store.save(store.create(projects.parse_template(FILLED), chat=7))  # state: proposing
    done = _run(rt, "project_step", {"project": "office-chat", "action": "go"})  # e.g. an old Go button
    assert isinstance(done, Done) and done.result["refused"] is True
    assert "proposing (version 1)" in done.result["answer"] and "gate" not in done.result
    assert store.get("office-chat").state == "proposing"
    unknown = _run(rt, "project_step", {"project": "nope", "action": "go"})
    assert "no single project matches" in unknown.result["answer"]


def test_gates_record_the_state_and_offer_buttons(tmp_path: Path):
    rt = _rt(tmp_path)
    store = projects.Projects(rt.paths)
    p = store.create(projects.parse_template(FILLED), chat=7)
    Path(p.dir, ".law").mkdir(parents=True)
    Path(p.dir, ".law", "PLAN.md").write_text("## Proposal v1 sections\n- [ ] appendix\n"
                                              "## Build version 1\n- [x] backend\n- [ ] frontend UI\n")
    store.save(p)
    done = _run(rt, "project_gate", {"project": p.id, "gate": "proposal", "_carry": {"review": {"score": 5}}})
    assert isinstance(done, Done) and done.result["gate"]["actions"] == ["go", "nogo"]
    assert "5/5" in done.result["answer"] and store.get(p.id).state == "awaiting_go"
    done = _run(rt, "project_gate", {"project": p.id, "gate": "verify", "_carry": {"review": {"score": 4}}})
    assert done.result["gate"]["actions"] == ["allgood", "changes"] and "frontend UI" in done.result["answer"]
    assert "appendix" not in done.result["answer"]  # only the build's unfinished milestones are reported
    assert store.get(p.id).state == "awaiting_verification"


def test_plan_complete_counts_only_this_versions_build_section(tmp_path: Path):
    law = tmp_path / ".law"
    law.mkdir()
    assert not plan_complete(tmp_path, 1)
    (law / "PLAN.md").write_text("## Build version 1\n- [x] a\n- [x] b\n")
    assert plan_complete(tmp_path, 1) and not plan_complete(tmp_path, 2)
    (law / "PLAN.md").write_text("## Build version 1\n- [x] a\n## Build version 2\n- [ ] c\n")
    assert plan_complete(tmp_path, 1) and not plan_complete(tmp_path, 2)
    # a finished proposal checklist for v2 must not look like a finished v2 build
    (law / "PLAN.md").write_text("## Proposal v2 sections\n- [x] summary\n## Build version 1\n- [x] a\n")
    assert not plan_complete(tmp_path, 2)
    # unticked research steps elsewhere don't block a finished build
    (law / "PLAN.md").write_text("## Research\n- [ ] more sources\n## Build version 2\n- [x] c\n### Notes\nx\n")
    assert plan_complete(tmp_path, 2)


def test_project_env_and_thinking_level_reach_the_agent(tmp_path: Path):
    (tmp_path / ".law").mkdir()
    (tmp_path / ".law" / "env.sh").write_text('export JAVA_HOME=/opt/jdk21\nexport PATH="/opt/jdk21/bin:$PATH"\n')
    env = project_env(tmp_path)
    assert env["JAVA_HOME"] == "/opt/jdk21" and env["PATH"].startswith("/opt/jdk21/bin:")
    assert project_env(tmp_path / "nope") == {}
    rt = _rt(tmp_path)
    rt.paths.config.mkdir(parents=True, exist_ok=True)
    (rt.paths.config / "opencode-coder-xhigh.json").write_text("{}")
    assert agent_env(rt, "coder", "xhigh")["OPENCODE_CONFIG"].endswith("opencode-coder-xhigh.json")
    assert agent_env(rt, "coder", "low")["OPENCODE_CONFIG"].endswith("opencode-coder.json")  # no such file
    assert agent_env(rt, "coder")["COLIMA_HOME"] == str(rt.paths.home / "colima")  # never a second VM
    assert agent_env(rt, "coder")["DOCKER_HOST"].endswith("/colima/default/docker.sock")


def test_cloning_uses_the_repo_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _rt(tmp_path)
    calls: list[list[str]] = []

    async def fake_run_cmd(ctx: Any, cmd: list[str], **kw: Any) -> str:
        calls.append(cmd)
        Path(cmd[-1]).mkdir(parents=True)  # noqa: ASYNC240 - the clone creates the folder
        return ""

    monkeypatch.setattr(project_handlers, "run_cmd", fake_run_cmd)
    fields = projects.parse_template(FILLED.replace("repo_url:", "repo_url: https://github.com/me/office-chat.git"))
    _run(rt, "project_new", {"fields": fields, "chat": 7})
    assert calls[0][:2] == ["git", "clone"] and calls[0][2] == "https://github.com/me/office-chat.git"


def test_tickets_under_milestones_decide_completion(tmp_path: Path):
    law = tmp_path / ".law"
    law.mkdir()
    board = ("## Build version 1\n### M1 Backend API\n- [x] LC-1 domain\n- [x] LC-2 join endpoint\n"
             "### M2 Frontend\n- [ ] LC-3 chat screen\n## Proposal v2 sections\n- [x] summary\n")
    (law / "PLAN.md").write_text(board)
    assert not plan_complete(tmp_path, 1)  # an unticked ticket in a later milestone keeps the build open
    (law / "PLAN.md").write_text(board.replace("- [ ] LC-3", "- [x] LC-3"))
    assert plan_complete(tmp_path, 1)


def test_development_spec_asks_for_a_ticket_board(tmp_path: Path):
    rt = _rt(tmp_path)
    store = projects.Projects(rt.paths)
    p = store.create(projects.parse_template(FILLED), chat=7)
    p.state = "awaiting_go"
    store.save(p)
    out = _run(rt, "project_step", {"project": "office-chat", "action": "go"})
    spec = next(specs.resolve(s["payload"]) for s in out.specs if s["payload"].get("milestone"))
    assert "### M1" in spec and '"OC-<n>: <summary>"' in spec and "## Build version 1" in spec

CODE_FILLED = FILLED.replace("/code new", "/code new").replace("thinking: high", "thinking: low")


def test_code_projects_plan_in_the_chat_then_build_without_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _rt(tmp_path)
    out = _run(rt, "project_new", {"fields": projects.parse_template(CODE_FILLED), "chat": 7, "mode": "code"})
    assert [s["kind"] for s in out.specs] == ["code_plan", "project_gate"]  # no research, proposal or email
    store = projects.Projects(rt.paths)
    assert store.get("office-chat").mode == "code"

    async def fake_answer(*a: Any, **k: Any) -> str:
        return "**What I'll build**: an office chat.\n**Stack**: Spring Boot 4.1 on JDK 21."

    monkeypatch.setattr(project_handlers, "answer", fake_answer)
    plan = _run(rt, "code_plan", {"project": "office-chat"})
    assert "Spring Boot 4.1" in plan.result["plan"]
    assert "Spring Boot 4.1" in (Path(store.get("office-chat").dir) / ".law" / "PROMPT.md").read_text()
    gate = _run(rt, "project_gate", {"project": "office-chat", "gate": "plan", "_carry": plan.result})
    assert gate.result["gate"]["actions"] == ["go", "nogo"] and "Plan v1: Office Chat" in gate.result["answer"]
    dev = _run(rt, "project_step", {"project": "office-chat", "action": "go"})
    kinds = [s["kind"] for s in dev.specs]
    assert "proposal_render" not in kinds and kinds[-1] == "project_gate"  # no handover document
    assert ".law/PROMPT.md" in specs.resolve(dev.specs[1]["payload"])  # builds from the agreed plan


def test_a_no_go_on_a_code_plan_revises_the_plan(tmp_path: Path):
    rt = _rt(tmp_path)
    store = projects.Projects(rt.paths)
    p = store.create(projects.parse_template(CODE_FILLED), chat=7, mode="code")
    p.state = "awaiting_go"
    store.save(p)
    out = _run(rt, "project_step", {"project": "office-chat", "action": "nogo", "note": "use Postgres"})
    assert [s["kind"] for s in out.specs] == ["code_plan", "project_gate"]
    assert store.get("office-chat").version == 2 and "use Postgres" in store.get("office-chat").request()


def test_email_resends_the_latest_document_and_unsent_gates_offer_it(tmp_path: Path):
    rt = _rt(tmp_path)
    store = projects.Projects(rt.paths)
    p = store.create(projects.parse_template(FILLED), chat=7)
    store.save(p)
    assert "no document" in _run(rt, "project_step", {"project": "office-chat", "action": "email"}).result["answer"]
    docs = Path(p.dir) / "docs" / "proposal"
    docs.mkdir(parents=True)
    (docs / "proposal-v2.pdf").write_bytes(b"%PDF")
    out = _run(rt, "project_step", {"project": "office-chat", "action": "email"})
    assert out.specs[0]["kind"] == "proposal_mail" and out.carry["pdf"].endswith("proposal-v2.pdf")
    gate = _run(rt, "project_gate", {"project": "office-chat", "gate": "proposal",
                                     "_carry": {"pdf": str(docs / "proposal-v2.pdf"), "emailed": None}})
    assert gate.result["gate"]["actions"] == ["go", "nogo", "email"]
