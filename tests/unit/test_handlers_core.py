"""Quality loop: draft -> independent judge -> revise only when the score is below the bar."""

from __future__ import annotations

import json
from pathlib import Path

import fakeredis
import pytest

from law import registry, roles
from law.app import handlers, resolver
from law.handlers.base import Runtime
from law.handlers.core import parse_verdict
from law.llm import LLM
from law.paths import LawPaths
from law.queue.jobs import Job, Status
from law.queue.scheduler import Capacity
from law.queue.store import JobStore
from law.queue.worker import Worker

REPO = Path(__file__).resolve().parents[2]


class FakeLLM(LLM):
    """Answers from canned rules at the `complete` seam, so the real chat/tool-loop code is exercised."""

    def __init__(self, scores: list[int]) -> None:
        super().__init__("http://fake/v1")
        self.scores = scores
        self.calls: list[tuple[str, object]] = []

    async def complete(self, body, on_thought=None):  # type: ignore[no-untyped-def,override]
        if on_thought:
            await on_thought("Checking the numbers.")
        user = body["messages"][-1]["content"]
        self.calls.append((body["model"], user))
        text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user if isinstance(p, dict))
        if "Score the RESPONSE" in text:
            s = self.scores.pop(0)
            content = f'```json\n{{"score": {s}, "issues": ["missing risk section"], "summary": "ok"}}\n```'
        elif "A reviewer found these problems" in text:
            content = "improved answer with risk section"
        else:
            content = "first draft"
        return {"message": {"content": content}, "finish_reason": "stop"}


@pytest.fixture()
async def setup(sandbox):
    paths = LawPaths(repo=REPO, home=sandbox / "LAW", user_home=sandbox)
    reg = registry.load(paths.registry_file)
    rt = Runtime(paths, reg, roles.load(paths.settings / "roles.yaml"), FakeLLM([]))  # type: ignore[arg-type]
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    w = Worker(store, handlers(rt), Capacity(budget_gb=48), resolver(rt))
    yield rt, store, w
    await store.close()


async def run_ask(rt, store, w, scores):
    rt.llm.scores = scores
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "Assess vendor X", "role": "architect"})))
    await w.drain()
    return await store.require(job.id)


async def test_good_draft_is_not_revised(setup):
    rt, store, w = setup
    done = await run_ask(rt, store, w, [5])
    assert done.status == Status.DONE
    assert done.result["answer"] == "first draft" and done.result["revised"] is False


async def test_weak_draft_is_revised_with_judge_feedback(setup):
    rt, store, w = setup
    done = await run_ask(rt, store, w, [2])
    assert done.result["answer"] == "improved answer with risk section"
    assert done.result["verdict"]["score"] == 2 and done.result["revised"] is True
    revise_prompt = rt.llm.calls[-1][1]
    assert "missing risk section" in revise_prompt


async def test_judge_uses_a_different_model_family_than_the_generator(setup):
    rt, store, w = setup
    await run_ask(rt, store, w, [5])
    draft_model, judge_model = rt.llm.calls[0][0], rt.llm.calls[1][0]
    assert draft_model.startswith("qwen") and judge_model.startswith("gemma")


async def test_resolver_sets_resources_from_registry(setup):
    rt, _, _ = setup
    j = resolver(rt)(Job(kind="task", payload={"role": "coder"}))
    assert j.resource == "llm:qwen3.8-27b-8bit" and j.lane == "heavy" and j.mem_gb == 40
    s = resolver(rt)(Job(kind="search", payload={}))
    assert s.lane == "none"


@pytest.mark.parametrize("text,score", [
    ('{"score": 4, "issues": []}', 4),
    ('noise {"score": "5"} trailing', 5),
    ("no json at all", 0),
    ('{"score": 9}', 5),
    ('{"score": "bad"}', 0),
])
def test_parse_verdict(text, score):
    assert parse_verdict(text)["score"] == score


async def test_image_reaches_the_vision_model_in_draft_and_revise(setup, sandbox):
    rt, store, w = setup
    img = sandbox / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    sent: list[object] = []
    original = rt.llm.complete

    async def spy(body, on_thought=None):  # type: ignore[no-untyped-def]
        sent.append(body["messages"][-1]["content"])
        return await original(body, on_thought)

    rt.llm.complete = spy
    rt.llm.scores = [2]
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "What is this?", "image": str(img)})))
    await w.drain()
    assert (await store.require(job.id)).status == Status.DONE
    with_image = [c for c in sent if isinstance(c, list)]
    assert len(with_image) == 2  # draft and revise see the image; the text judge does not
    assert all(any(p.get("type") == "image_url" for p in c) for c in with_image)


async def test_image_to_non_vision_role_fails_clearly(setup, sandbox):
    rt, store, w = setup
    img = sandbox / "photo.png"
    img.write_bytes(b"\x89PNG")
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "x", "role": "reviewer", "image": str(img)})))
    await w.drain()
    done = await store.require(job.id)
    assert done.status == Status.FAILED and "cannot see images" in (done.error or "")


def test_agent_runs_keep_data_under_law_home(setup):
    from law.handlers.tasks import agent_env

    rt, _, _ = setup
    env = agent_env(rt, "coder")
    assert env["OPENCODE_CONFIG"].endswith("opencode-coder.json")
    assert env["MAVEN_OPTS"] == f"-Dmaven.repo.local={rt.paths.cache / 'm2'}"
    for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "npm_config_cache", "GRADLE_USER_HOME"):
        assert env[key].startswith(str(rt.paths.home)), key


def _opencode_json(text: str) -> str:
    import json as _json
    return _json.dumps({"type": "text", "part": {"type": "text", "text": text}})


async def _run_task(setup, sandbox, monkeypatch, review_score: int):
    from law.handlers import tasks

    rt, store, w = setup
    calls: list[tuple[str, str]] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        role, prompt = cmd[cmd.index("--agent") + 1], cmd[-1]
        calls.append((role, prompt))
        if role == "reviewer":
            return _opencode_json(f'{{"score": {review_score}, "issues": ["calc.py:17 docstring says decimal, code uses percent"], "summary": "x"}}')
        return _opencode_json("built it; just check green")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    job = await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "build invoice calc", "role": "coder",
                                                                        "dir": str(sandbox)})))
    await w.drain()
    return await store.require(job.id), calls


async def test_coder_task_gets_independent_review_and_fix(setup, sandbox, monkeypatch):
    done, calls = await _run_task(setup, sandbox, monkeypatch, review_score=3)
    assert done.status == Status.DONE and done.result["fixed"] is True
    assert [c[0] for c in calls] == ["coder", "reviewer", "coder"]
    assert "docstring says decimal" in calls[2][1]  # the fix pass receives the reviewer's findings


async def test_good_work_is_not_reworked(setup, sandbox, monkeypatch):
    done, calls = await _run_task(setup, sandbox, monkeypatch, review_score=5)
    assert done.result["fixed"] is False and [c[0] for c in calls] == ["coder", "reviewer"]


async def test_reviewer_runs_on_a_different_model_family(setup):
    from law.app import resolver as res
    from law.queue.jobs import Job as J

    rt, _, _ = setup
    assert "gemma" in res(rt)(J(kind="task_review", payload={})).resource
    assert "qwen" in res(rt)(J(kind="task_work", payload={"role": "coder"})).resource


async def test_explicit_directory_is_given_to_the_agent(setup, sandbox, monkeypatch):
    _, calls = await _run_task(setup, sandbox, monkeypatch, review_score=5)
    assert f"Work in this directory and create the project files directly in it: {sandbox}" in calls[0][1]


async def test_truncated_thinking_retries_at_lower_effort(setup):
    from law.handlers.core import answer

    rt, _, _ = setup
    efforts: list[str] = []

    async def complete(body, on_thought=None):  # type: ignore[no-untyped-def]
        efforts.append(body.get("reasoning_effort"))
        if len(efforts) == 1:
            return {"message": {"content": ""}, "finish_reason": "length"}
        return {"message": {"content": "39356"}, "finish_reason": "stop"}

    rt.llm.complete = complete
    assert await answer(rt, "architect", [{"role": "user", "content": "x"}]) == "39356"
    assert efforts == ["xhigh", "medium"]


def test_every_role_has_a_reasoning_effort():
    from law import roles

    loaded = roles.load(REPO / "settings" / "roles.yaml")
    assert {r.reasoning for r in loaded.roles.values()} <= {"off", "low", "medium", "xhigh"}
    assert loaded.get("operator").reasoning == "low"


@pytest.mark.parametrize("text,seconds,rest", [
    ("90s upbeat Malay pop", 90, "upbeat Malay pop"),
    ("lofi beat, 2 min, rainy mood", 120, "lofi beat, rainy mood"),
    ("epic orchestral 10 minutes", 240, "epic orchestral"),  # clamped
    ("calm piano", 60, "calm piano"),
])
def test_parse_duration(text, seconds, rest):
    from law.handlers.media import parse_duration

    assert parse_duration(text, 60, 15, 240) == (seconds, rest)


def test_split_lyrics_keeps_user_lyrics_verbatim():
    from law.handlers.media import split_lyrics

    style, lyrics = split_lyrics("acoustic folk, warm female vocal\nlyrics:\n[verse]\nRain on Jalan Alor\n[chorus]\nStay")
    assert style == "acoustic folk, warm female vocal"
    assert lyrics == "[verse]\nRain on Jalan Alor\n[chorus]\nStay"
    assert split_lyrics("just a beat") == ("just a beat", None)


async def test_live_thoughts_reach_the_job_the_user_watches(setup):
    rt, store, w = setup
    rt.llm.scores = [5]
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "x"})))
    await w.drain()
    messages = [e["data"].get("message") for e in await store.events(job.id) if e["event"] == "progress"]
    assert "💭 Checking the numbers." in messages  # emitted by the draft stage, visible on the root


def test_latest_thought_picks_the_last_complete_sentence():
    from law.llm import latest_thought

    assert latest_thought("First I compute r. Then n = 360. Now the formu") == "Then n = 360."
    assert latest_thought("Done thinking.") == "Done thinking."
    assert latest_thought("Year 0 is 3.3. Year 1: Revenue = 12.4 x 1.18 = 14.") == "Year 0 is 3.3."
    assert latest_thought("Year 0 is 3.3. Year 1 is 4.53. ") == "Year 1 is 4.53."
    assert latest_thought("") == ""


@pytest.mark.parametrize("event,expected", [
    ({"part": {"type": "tool", "tool": "write", "state": {"status": "completed",
               "input": {"filePath": "{HOME}/SourceCode/lan-chat/backend/pom.xml"}}}},
     "✏️ writing ~/SourceCode/lan-chat/backend/pom.xml"),
    ({"part": {"type": "tool", "tool": "bash", "state": {"status": "completed",
               "input": {"command": "./mvnw verify\nmore"}}}}, "▶ running ./mvnw verify ; more"),
    ({"part": {"type": "tool", "tool": "bash", "state": {"status": "completed",
               "input": {"command": "\n  osascript <<'EOF'\ntell app \"TextEdit\""}}}}, "▶ running osascript <<'EOF'"),
    ({"part": {"type": "tool", "tool": "mac_set_volume", "state": {"status": "completed", "input": {}}}}, "🖥 set volume"),
    ({"part": {"type": "tool", "tool": "bash", "state": {"status": "running", "input": {"command": "x"}}}}, None),
    ({"part": {"type": "text", "text": "hi"}}, None),
])
def test_describe_opencode_line(event, expected):
    import json as _json

    from law.handlers.tasks import describe_opencode_line

    line = _json.dumps(event).replace("{HOME}", str(Path.home()))
    assert describe_opencode_line(line) == expected
    assert describe_opencode_line("not json") is None


def _tool_event(tool: str, target: str, session: str = "ses_1") -> str:
    import json as _json
    return _json.dumps({"type": "tool_use", "sessionID": session,
                        "part": {"type": "tool", "tool": tool,
                                 "state": {"status": "completed", "input": {"filePath": target}}}})


def test_only_file_changes_count_as_agent_progress():
    from law.handlers.tasks import is_file_change, session_id

    assert is_file_change(_tool_event("edit", "/x/User.java"))
    assert not is_file_change(_tool_event("read", "/x/User.java"))
    assert not is_file_change("plain log line")
    assert session_id("noise\n" + _tool_event("read", "/a", "ses_9")) == "ses_9"
    assert session_id("no json here") is None


async def test_stalled_agent_is_resumed_with_a_nudge(setup, sandbox, monkeypatch):
    from law.handlers import tasks
    from law.handlers.base import Stalled

    rt, store, w = setup
    cmds: list[list[str]] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        cmds.append(cmd)
        assert kw["stall_s"] == 25 * 60
        if len(cmds) == 1:
            raise Stalled(_tool_event("read", "/a", "ses_42"), 1500)
        return _opencode_json("fixed and committed")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    job = await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "fix build", "role": "coder",
                                                                        "dir": str(sandbox), "review": False})))
    await w.drain()
    done = await store.require(job.id)
    assert done.status == Status.DONE and done.result["answer"] == "fixed and committed"
    assert cmds[1][cmds[1].index("--session") + 1] == "ses_42"
    assert "Stop reading" in cmds[1][-1]


async def test_an_agent_that_keeps_stalling_fails(setup, sandbox, monkeypatch):
    from law.handlers import tasks
    from law.handlers.base import Stalled

    rt, store, w = setup
    calls = 0

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise Stalled(_tool_event("read", "/a"), 1500)

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    job = await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "fix build", "role": "coder",
                                                                        "dir": str(sandbox), "review": False})))
    await w.drain()
    done = await store.require(job.id)
    assert done.status == Status.FAILED and "stalled" in (done.error or "")
    assert calls == tasks.STALL_NUDGES + 1


async def test_nudges_after_real_progress_do_not_use_up_the_budget(setup, sandbox, monkeypatch):
    """One stage runs a whole milestone: tickets committed between nudges earn a fresh budget."""
    from law.handlers import tasks
    from law.handlers.base import Stalled

    rt, store, w = setup
    calls = 0

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls <= tasks.STALL_NUDGES + 2:  # more stalls than the budget, each after saving a file
            raise Stalled(_tool_event("write", f"/a/T{calls}.java", "ses_7"), 1500)
        return _opencode_json("milestone done")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    job = await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "build M1", "role": "coder",
                                                                        "dir": str(sandbox), "review": False})))
    await w.drain()
    done = await store.require(job.id)
    assert done.status == Status.DONE and calls == tasks.STALL_NUDGES + 3


async def test_reviewer_is_never_treated_as_stalled(setup, sandbox, monkeypatch):
    from law.handlers import tasks

    seen: dict[str, object] = {}

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        seen[cmd[cmd.index("--agent") + 1]] = kw["stall_s"]
        if "reviewer" in cmd:
            return _opencode_json('{"score": 5, "issues": [], "summary": "ok"}')
        return _opencode_json("done")

    rt, store, w = setup
    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "x", "role": "coder", "dir": str(sandbox)})))
    await w.drain()
    assert seen == {"coder": 25 * 60, "reviewer": None}


async def test_translated_brief_drives_the_agent_and_lands_in_project_memory(setup, sandbox, monkeypatch):
    from law.handlers import tasks

    rt, store, w = setup
    prompts: list[tuple[str, str]] = []

    async def fake_answer(rt_, role, messages, ctx=None):  # type: ignore[no-untyped-def]
        prompts.append((role, messages[-1]["content"]))
        return "## Goal\nA LAN chat app\n## Acceptance criteria\n- ./mvnw verify passes"

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        ctx_line = _tool_event("edit", "/x", "ses_7")
        kw["describe"](ctx_line)  # the agent's first event records its session
        prompts.append((cmd[cmd.index("--agent") + 1], cmd[-1]))
        return _opencode_json("done")

    monkeypatch.setattr(tasks, "answer", fake_answer)
    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    (sandbox / "pom.xml").write_text("<project/>")
    job = await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "make the chat app work",
                                                                        "role": "coder", "dir": str(sandbox),
                                                                        "review": False})))
    await w.drain()
    assert (await store.require(job.id)).status == Status.DONE
    (t_role, t_prompt), (a_role, a_prompt) = prompts
    assert t_role == "translator" and "make the chat app work" in t_prompt and "pom.xml" in t_prompt
    assert a_role == "coder" and "## Acceptance criteria" in a_prompt and ".law/PROGRESS.md" in a_prompt
    law = sandbox / ".law"
    assert "./mvnw verify passes" in (law / "PROMPT.md").read_text()
    assert "make the chat app work" in (law / "REQUESTS.md").read_text()
    assert json.loads((law / "session.json").read_text())["session"] == "ses_7"


async def test_restarted_job_resumes_its_recorded_session(setup, sandbox, monkeypatch):
    from law.handlers import tasks
    from law.project_memory import ProjectMemory

    rt, store, w = setup
    cmds: list[list[str]] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        cmds.append(cmd)
        return _opencode_json("continued")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    job = resolver(rt)(Job(kind="task", payload={"prompt": "x", "role": "coder", "dir": str(sandbox),
                                                  "review": False, "translate": False}))
    ProjectMemory(sandbox).save_session(job.id, "coder", "ses_old")  # the session that died
    await store.submit(job)
    await w.drain()
    assert cmds[0][cmds[0].index("--session") + 1] == "ses_old" and "interrupted" in cmds[0][-1]


async def test_new_project_without_folder_is_told_to_create_its_memory(setup, sandbox, monkeypatch):
    from law.handlers import tasks

    rt, store, w = setup
    rt.paths.projects.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        seen.append(cmd[-1])
        return _opencode_json("done")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "build a todo app", "role": "coder",
                                                                "review": False, "translate": False})))
    await w.drain()
    assert "Work in this directory" not in seen[0]  # no folder named: the agent makes its own project folder
    assert "create .law/ first" in seen[0] and seen[0].endswith("build a todo app")
    assert not (rt.paths.projects / ".law").exists()


async def test_translator_runs_on_the_same_model_as_the_coder(setup):
    rt, _, _ = setup
    translate = resolver(rt)(Job(kind="task_translate", payload={"role": "coder"}))
    work = resolver(rt)(Job(kind="task_work", payload={"role": "coder"}))
    assert translate.resource == work.resource  # no model swap between the two stages


@pytest.mark.parametrize("line,expected", [
    ("Project folder: {root}/lan-chat", "lan-chat"),
    ("**Project folder:** `{root}/Invoice-API`", "invoice-api"),
    ("Project folder: invoice-api", "invoice-api"),
    ("Project folder: {root}", None),  # never the projects root itself
    ("Project folder: {root}/a/b", None),  # only directly under the root
    ("Project folder: /tmp/elsewhere", None),
    ("no folder line", None),
])
def test_project_folder_is_validated(tmp_path, line, expected):
    from law.handlers.tasks import project_folder

    got = project_folder(line.format(root=tmp_path) + "\n## Goal\nx", tmp_path)
    assert got == (tmp_path / expected if expected else None)


async def test_new_project_gets_the_folder_the_translator_names(setup, sandbox, monkeypatch):
    from law.handlers import tasks

    rt, store, w = setup
    (rt.paths.projects / "lan-chat").mkdir(parents=True)
    seen: dict[str, str] = {}

    async def fake_answer(rt_, role, messages, ctx=None):  # type: ignore[no-untyped-def]
        seen["translator"] = messages[-1]["content"]
        return f"Project folder: {rt.paths.projects}/invoice-api\n## Goal\nAn invoice API"

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        seen["coder"] = cmd[-1]
        seen["cwd"] = str(kw["cwd"])
        return _opencode_json("done")

    monkeypatch.setattr(tasks, "answer", fake_answer)
    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    job = await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "build the invoice-api project",
                                                                        "role": "coder", "review": False})))
    await w.drain()
    done = await store.require(job.id)
    target = rt.paths.projects / "invoice-api"
    assert done.status == Status.DONE and done.result["dir"] == str(target)
    assert "lan-chat" in seen["translator"]  # it knows the existing projects
    assert seen["cwd"] == str(target) and f"directly in it: {target}" in seen["coder"]
    assert (target / ".law" / "PROMPT.md").exists() and not (rt.paths.projects / "lan-chat" / ".law").exists()


async def test_translator_without_a_valid_folder_fails_the_job(setup, monkeypatch):
    from law.handlers import tasks

    rt, store, w = setup
    rt.paths.projects.mkdir(parents=True, exist_ok=True)

    async def fake_answer(rt_, role, messages, ctx=None):  # type: ignore[no-untyped-def]
        return f"Project folder: {rt.paths.projects}\n## Goal\nx"

    monkeypatch.setattr(tasks, "answer", fake_answer)
    job = await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "build x", "role": "coder"})))
    await w.drain()
    done = await store.require(job.id)
    assert done.status == Status.FAILED and "valid project folder" in (done.error or "")


def test_fetch_progress_names_the_site_not_a_clickable_url():
    import json as _json

    from law.handlers.tasks import describe_opencode_line, is_file_change

    def fetch(url: str) -> str:
        return _json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "webfetch",
                                                         "state": {"status": "completed", "input": {"url": url}}}})

    assert describe_opencode_line(fetch("https://bridgemessenger.app/")) == "🌐 reading bridgemessenger.app"
    assert describe_opencode_line(fetch("http://127.0.0.1:8888/search?q=x&format=json")) == "🌐 reading web search"
    assert is_file_change(fetch("https://example.com/a"))  # fetching still counts as research progress


async def test_a_stalled_writer_is_told_to_save_sections_not_to_build(setup, sandbox, monkeypatch):
    from law.handlers import tasks
    from law.handlers.base import Stalled

    rt, store, w = setup
    cmds: list[list[str]] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        cmds.append(cmd)
        if len(cmds) == 1:
            raise Stalled(_tool_event("read", "/a", "ses_w"), 1500)
        return _opencode_json("saved")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "write a memo", "role": "writer",
                                                              "dir": str(sandbox), "review": False,
                                                              "translate": False})))
    await w.drain()
    assert "one section" in cmds[1][-1] or "section by section" in cmds[1][-1]
    assert "build or tests" not in cmds[1][-1]


@pytest.mark.parametrize("command,dig", [
    ('▶ running JAR=~/LAW/cache/m2/repository/org/springframework/spring-websocket/7.0.9/spring-websocket-7.0.9.jar; javap -classpath "$JAR" org.springframe', True),
    ('▶ running find ~/LAW/cache/m2/repository/org/springframework/spring-websocket -name "*.jar" 2>/dev/null', True),
    ('▶ running M2=~/LAW/cache/m2/org/springframework; MSG=$(find $M2/spring-messaging -name "spring-messaging-7*.ja', True),
    ('▶ running find / -name "spring-messaging-*.jar" 2>/dev/null | head', True),
    ("▶ running mvn -q help:evaluate -Dexpression=settings.localRepository -DforceStdout", True),
    ("▶ running unzip -l target/app.jar | grep BOOT-INF", False),
    ("▶ running find src -name '*.java' | head", False),
    ("▶ running law api StompSession subscribe", False),
    ("▶ running grep -rn 'export declare class HttpClient' node_modules/@angular/common/types", True),
    ("▶ running cat ~/go/pkg/mod/github.com/gorilla/websocket@v1.5.3/client.go | head -80", True),
    ("▶ running grep -n NavigationStack /Applications/Xcode.app/Contents/Developer/Platforms/iPhoneSimulator."
     "platform/Developer/SDKs/iPhoneSimulator.sdk/x.swiftinterface", True),
    ("▶ running rm -rf node_modules && npm ci", False),
    ("▶ running node_modules/.bin/ng test --watch=false | tail -40", False),
    ("▶ running npx ng build 2>&1 | tail -30", False),
    ("▶ running source .law/env.sh && mvn -q -B test 2>&1 | grep -E 'Tests run|ERROR'", False),
    ("▶ running source .law/env.sh && mvn -B dependency:tree", False),
])
def test_library_digging_is_recognised(command, dig):
    from law.handlers.tasks import is_library_dig

    assert is_library_dig(command) is dig


async def test_a_coder_reading_library_internals_is_redirected(setup, sandbox, monkeypatch):
    from law.handlers import tasks
    from law.handlers.base import Steered

    rt, store, w = setup
    cmds: list[list[str]] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        cmds.append(cmd)
        if len(cmds) == 1:
            steer = kw["steer"]
            assert steer("▶ running mvn -q test") is None
            for _ in range(2):
                assert steer("▶ running javap -classpath x.jar Foo") is None
            message = steer("▶ running javap -c x.jar Foo")
            raise Steered(_tool_event("bash", "javap", "ses_dig"), message)
        return _opencode_json("LC-2 committed")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "build", "role": "coder", "dir": str(sandbox),
                                                              "review": False, "translate": False})))
    await w.drain()
    assert cmds[1][cmds[1].index("--session") + 1] == "ses_dig" and "Stop reading library internals" in cmds[1][-1]
    assert "law api" in cmds[1][-1]


def test_command_summaries_skip_boilerplate_and_count_lookups():
    import json as _json

    from law.handlers.tasks import describe_opencode_line

    def bash(command: str) -> str:
        return _json.dumps({"part": {"type": "tool", "tool": "bash",
                                     "state": {"status": "completed", "input": {"command": command}}}})

    cmd = ('source .law/env.sh\necho "=== Stomp ==="; law api StompHeaderAccessor fromMessage 2>&1 | head -8\n'
           'echo "=== Chan ==="; law api ChannelInterceptor')
    summary = describe_opencode_line(bash(cmd))
    assert summary is not None and summary.startswith("▶ running echo") is False
    assert "law api StompHeaderAccessor" in summary and summary.count("law api ") == 2
    assert describe_opencode_line(bash("source .law/env.sh && mvn -q test")) == "▶ running mvn -q test"


async def test_a_coder_that_only_looks_up_apis_is_told_to_write_code(setup, sandbox, monkeypatch):
    from law.handlers import tasks
    from law.handlers.base import Steered

    rt, store, w = setup
    cmds: list[list[str]] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        cmds.append(cmd)
        if len(cmds) == 1:
            steer = kw["steer"]
            for _ in range(tasks.LOOKUP_LIMIT - 2):
                assert steer("▶ running law api A x") is None
            assert steer("✏️ writing ~/p/A.java") is None  # a saved file resets the budget
            for _ in range(tasks.LOOKUP_LIMIT - 1):
                assert steer("▶ running law api B y") is None
            message = steer("▶ running law api C z")
            assert message == tasks.LOOKUP_NUDGE
            raise Steered(_tool_event("bash", "law api", "ses_look"), message)
        return _opencode_json("LC-3 committed")

    monkeypatch.setattr(tasks, "run_cmd", fake_run_cmd)
    await store.submit(resolver(rt)(Job(kind="task", payload={"prompt": "build", "role": "coder", "dir": str(sandbox),
                                                              "review": False, "translate": False})))
    await w.drain()
    assert cmds[1][cmds[1].index("--session") + 1] == "ses_look" and "looked up enough" in cmds[1][-1]
