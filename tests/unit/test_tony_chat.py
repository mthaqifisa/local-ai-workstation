"""Tony's chat (ADR-0017): conversation history, hand-off to agents, quick replies and review of long answers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from law import registry, roles
from law.app import handlers, resolver
from law.handlers.base import Runtime
from law.llm import LLM
from law.paths import LawPaths
from law.queue.jobs import Job, Status
from law.queue.scheduler import Capacity
from law.queue.store import JobStore
from law.queue.worker import Worker
from law.telegram_bot import ChatHistory

REPO = Path(__file__).resolve().parents[2]


class ScriptedLLM(LLM):
    """Replies from a script at the `complete` seam: a dict is a tool call, a string is the answer text.
    The judge always gets a fixed verdict."""

    def __init__(self, replies: list[Any], score: int = 5) -> None:
        super().__init__("http://fake/v1")
        self.replies = replies
        self.score = score
        self.bodies: list[dict[str, Any]] = []

    async def complete(self, body, on_thought=None):  # type: ignore[no-untyped-def,override]
        self.bodies.append(body)
        last = body["messages"][-1]["content"]
        if isinstance(last, str) and "Score the RESPONSE" in last:
            return {"message": {"content": json.dumps({"score": self.score, "issues": ["x"], "summary": "s"})},
                    "finish_reason": "stop"}
        reply = self.replies.pop(0)
        if isinstance(reply, dict):
            call = {"id": "c1", "type": "function",
                    "function": {"name": reply["name"], "arguments": json.dumps(reply["args"])}}
            return {"message": {"content": "", "tool_calls": [call]}, "finish_reason": "tool_calls"}
        return {"message": {"content": reply}, "finish_reason": "stop"}


@pytest.fixture()
async def setup(sandbox):
    paths = LawPaths(repo=REPO, home=sandbox / "LAW", user_home=sandbox)
    reg = registry.load(paths.registry_file)
    rt = Runtime(paths, reg, roles.load(paths.settings / "roles.yaml"), ScriptedLLM([]))  # type: ignore[arg-type]
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    w = Worker(store, handlers(rt), Capacity(budget_gb=48), resolver(rt))
    yield rt, store, w
    await store.close()


async def chat(rt, store, w, prompt: str, replies: list[Any], *, history=None, score: int = 5) -> Job:
    rt.llm.replies, rt.llm.score = replies, score
    job = Job(kind="ask", payload={"prompt": prompt, "role": "assistant", "history": history or []},
              source="telegram", reply_to={"chat_id": 42, "status_id": 7, "file_field": None, "then": None})
    job = await store.submit(resolver(rt)(job))
    await w.drain()
    return await store.require(job.id)


async def test_a_short_reply_comes_straight_back_from_the_fast_model(setup):
    rt, store, w = setup
    done = await chat(rt, store, w, "hey bro", ["Hey! What can I do for you?"])
    assert done.status == Status.DONE and done.result["answer"] == "Hey! What can I do for you?"
    assert done.result["review"] == "skipped: short reply"
    assert [b["model"] for b in rt.llm.bodies] == ["qwen3.6-35b-a3b-8bit"]  # one call, no judge, no swap


async def test_a_long_answer_is_still_reviewed_by_another_model_family(setup):
    rt, store, w = setup
    long = ("A detailed recommendation. " * 60).strip()
    done = await chat(rt, store, w, "Lease or buy laptops?", [long], score=5)
    assert done.result["answer"] == long and done.result["verdict"]["score"] == 5
    assert [b["model"] for b in rt.llm.bodies] == ["qwen3.6-35b-a3b-8bit", "gemma-4-31b-8bit"]


async def test_the_conversation_reaches_the_model_and_the_judge(setup):
    rt, store, w = setup
    history = [{"role": "user", "content": "Write me a PDF report on LAW v6"},
               {"role": "assistant", "content": "Started the writer agent: job abc123"}]
    await chat(rt, store, w, "Continue", ["The writer agent (job abc123) is still on it. " * 30], history=history)
    draft = rt.llm.bodies[0]["messages"]
    assert draft[1:3] == history and draft[-1] == {"role": "user", "content": "Continue"}
    judge = rt.llm.bodies[1]["messages"][-1]["content"]
    assert "EARLIER CONVERSATION" in judge and "Write me a PDF report on LAW v6" in judge


async def test_work_beyond_an_answer_is_handed_to_an_agent_that_reports_in_the_chat(setup, sandbox):
    rt, store, w = setup
    folder = sandbox / "SourceCode" / "doc"
    request = "Study ~/SourceCode/v6, write a PDF report in the doc folder, send it by Telegram and email"
    tool = {"name": "start_agent", "args": {"deliverable": "document", "request": request, "folder": str(folder)}}
    rt.llm.replies = [tool, "The writer agent is on it; progress will appear here."]
    job = Job(kind="ask", payload={"prompt": request, "role": "assistant"}, source="telegram",
              reply_to={"chat_id": 42, "status_id": 7, "file_field": None, "then": None})
    job = await store.submit(resolver(rt)(job))
    for _ in range(100):  # run only the chat; the agent task it queued is checked, not run
        await w.schedule_once()
        if (await store.require(job.id)).status == Status.DONE:
            break
        for t in list(w.tasks.values()):
            await t
    done = await store.require(job.id)
    assert done.result["review"].startswith("skipped: handed to an agent")
    agent = await store.require(done.result["agent_job"])
    assert agent.kind == "task" and agent.payload == {"role": "writer", "prompt": request, "dir": str(folder)}
    assert agent.reply_to == {"chat_id": 42, "status_id": None, "file_field": None, "then": None}
    assert agent.source == "telegram" and folder.is_dir()
    tool_result = rt.llm.bodies[1]["messages"][-1]
    assert tool_result["role"] == "tool" and f"started job {agent.id}" in tool_result["content"]


async def test_one_message_starts_at_most_one_agent(setup):
    rt, store, w = setup
    tool = {"name": "start_agent", "args": {"deliverable": "research", "request": "Research Apple Foundation Models"}}
    rt.llm.replies = [tool, tool, "On it."]
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "research it", "role": "assistant"})))
    while (await store.require(job.id)).status != Status.DONE:
        await w.schedule_once()
        for t in list(w.tasks.values()):
            await t
    tasks = [j for j in await store.queued() if j.kind == "task"]
    assert len(tasks) == 1 and tasks[0].payload["dir"].endswith(f"agents/{job.id}")
    assert "already started job" in rt.llm.bodies[2]["messages"][-1]["content"]


async def test_an_agent_folder_outside_the_home_folder_is_refused(setup):
    rt, store, w = setup
    tool = {"name": "start_agent", "args": {"deliverable": "document", "request": "x", "folder": "/etc/law"}}
    rt.llm.replies = [tool, "I could not start it."]
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "x", "role": "assistant"})))
    while (await store.require(job.id)).status != Status.DONE:
        await w.schedule_once()
        for t in list(w.tasks.values()):
            await t
    assert "error: folder must be an absolute path in" in rt.llm.bodies[1]["messages"][-1]["content"]
    assert not [j for j in await store.queued() if j.kind == "task"]


async def test_other_roles_cannot_start_agents(setup):
    rt, store, w = setup
    rt.llm.replies = ["An architecture answer. " * 50]
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "design", "role": "architect"})))
    await w.drain()
    assert (await store.require(job.id)).status == Status.DONE
    tools = rt.llm.bodies[0].get("tools") or []
    assert all(t["function"]["name"] != "start_agent" for t in tools)


def test_chat_history_alternates_trims_and_survives_restarts(tmp_path):
    h = ChatHistory(tmp_path, messages=4, chars=20)
    h.add(1, "assistant", "orphan answer")  # a conversation never starts with the assistant
    assert h.get(1) == []
    h.add(1, "user", "first")
    h.add(1, "user", "second")
    assert h.get(1) == [{"role": "user", "content": "first\n\nsecond"}]
    h.add(1, "assistant", "x" * 50)
    assert h.get(1)[-1]["content"] == "x" * 20 + " … (cut)"
    for n in range(4):
        h.add(1, "user", f"q{n}")
        h.add(1, "assistant", f"a{n}")
    again = ChatHistory(tmp_path, messages=4, chars=20)
    assert [m["content"] for m in again.get(1)] == ["q2", "a2", "q3", "a3"]
    assert again.get(2) == []
    again.clear(1)
    assert again.get(1) == []


def test_chat_history_remembers_answers_failures_and_agent_results(tmp_path):
    h = ChatHistory(tmp_path)
    h.remember(5, {"kind": "ask", "status": "done", "payload": {"prompt": "write the report"},
                   "result": {"answer": "Started the writer agent: job w1"}})
    h.remember(5, {"kind": "task", "id": "w1", "status": "done", "payload": {"role": "writer"},
                   "result": {"answer": "PDF at ~/doc/report.pdf; sent by Telegram and email"}})
    h.remember(5, {"kind": "ask", "status": "failed", "error": "model crashed", "payload": {"prompt": "continue"},
                   "result": None})
    msgs = h.get(5)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert "[task job w1 (writer agent) done] PDF at ~/doc/report.pdf" in msgs[1]["content"]
    assert msgs[3]["content"] == "(no answer: failed, model crashed)"


@pytest.mark.parametrize("deliverable,role", [("document", "writer"), ("software", "coder"),
                                              ("mac_task", "operator"), ("bogus", None)])
async def test_the_deliverable_picks_the_agent(setup, deliverable, role):
    rt, store, w = setup
    rt.llm.replies = [{"name": "start_agent", "args": {"deliverable": deliverable, "request": "do it"}}, "ok"]
    job = await store.submit(resolver(rt)(Job(kind="ask", payload={"prompt": "do it", "role": "assistant"})))
    while (await store.require(job.id)).status != Status.DONE:
        await w.schedule_once()
        for t in list(w.tasks.values()):
            await t
    tasks = [j for j in await store.queued() if j.kind == "task"]
    if role is None:
        assert not tasks and "error: deliverable must be one of" in rt.llm.bodies[1]["messages"][-1]["content"]
    else:
        assert [t.payload["role"] for t in tasks] == [role]
