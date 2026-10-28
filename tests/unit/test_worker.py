"""Worker behaviour against an in-memory Valkey (fakeredis)."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from law.queue.jobs import Job, Priority, Status
from law.queue.scheduler import Capacity
from law.queue.store import JobStore
from law.queue.worker import Context, Done, Fatal, Stages, Worker


def resolve(job: Job) -> Job:
    table = {"ask": ("llm:a", "heavy", 30.0), "judge": ("llm:j", "heavy", 30.0), "tts": ("tts", "light", 1.0),
             "search": ("", "none", 0.0), "boom": ("llm:a", "heavy", 30.0), "bad": ("", "none", 0.0),
             "slow": ("llm:a", "heavy", 30.0)}
    job.resource, job.lane, job.mem_gb = table[job.kind]  # type: ignore[assignment]
    return job


class Models:
    def __init__(self) -> None:
        self.loads: list[str] = []

    async def prepare(self, resource: str, resident: str | None) -> None:
        self.loads.append(resource)


@pytest.fixture()
async def store():
    s = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    yield s
    await s.close()


def make_worker(store: JobStore, handlers=None, models=None) -> Worker:
    attempts: dict[str, int] = {}

    async def ask(ctx: Context) -> Done:
        await ctx.progress("thinking")
        return Done({"answer": f"echo {ctx.job.payload.get('q')}"})

    async def boom(ctx: Context) -> Done:
        attempts[ctx.job.id] = attempts.get(ctx.job.id, 0) + 1
        if attempts[ctx.job.id] < 2:
            raise RuntimeError("model crashed")
        return Done({"ok": True})

    async def bad(ctx: Context) -> Done:
        raise Fatal("file not found")

    async def slow(ctx: Context) -> Done:
        for _ in range(200):
            ctx.check_cancel()
            await asyncio.sleep(0.01)
        return Done({})

    async def tts(ctx: Context) -> Done:
        return Done({"audio": "x.wav"})

    async def search(ctx: Context) -> Done:
        return Done({"hits": 3})

    async def judge(ctx: Context) -> Done:
        draft = ctx.job.payload["_carry"]["answer"]
        return Done({"verdict": "pass", "judged": draft})

    base = {"ask": ask, "boom": boom, "bad": bad, "slow": slow, "tts": tts, "search": search, "judge": judge}
    w = Worker(store, handlers or base, Capacity(budget_gb=48), resolve, models)
    return w


async def submit(store: JobStore, kind: str, **kw) -> Job:
    return await store.submit(resolve(Job(kind=kind, **kw)))


async def test_job_runs_to_done_with_events(store):
    w = make_worker(store)
    j = await submit(store, "ask", payload={"q": "hi"})
    await w.drain()
    done = await store.require(j.id)
    assert done.status == Status.DONE and done.result == {"answer": "echo hi"}
    kinds = [e["event"] for e in await store.events(j.id)]
    assert kinds == ["queued", "started", "progress", "progress", "done"]  # loading + thinking


async def test_retry_then_success(store, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    w = make_worker(store)
    j = await submit(store, "boom")
    await w.drain()
    done = await store.require(j.id)
    assert done.status == Status.DONE and done.attempts == 2


async def test_fatal_is_not_retried(store):
    w = make_worker(store)
    j = await submit(store, "bad")
    await w.drain()
    done = await store.require(j.id)
    assert done.status == Status.FAILED and done.attempts == 1 and "file not found" in (done.error or "")


async def test_cancel_running_job(store):
    w = make_worker(store)
    j = await submit(store, "slow")
    await w.schedule_once()
    w.cancel_events[j.id].set()
    await w.drain()
    assert (await store.require(j.id)).status == Status.CANCELLED


async def test_cancel_queued_job(store):
    w = make_worker(store)
    j = await submit(store, "ask")
    await store.request_cancel(j.id)
    await w.drain()
    assert (await store.require(j.id)).status == Status.CANCELLED


async def test_recover_requeues_jobs_from_dead_worker(store):
    w1 = make_worker(store)
    j = await submit(store, "slow")
    await w1.schedule_once()
    for t in w1.tasks.values():  # simulate kill -9: tasks vanish, Valkey still says running
        t.cancel()
    await asyncio.gather(*w1.tasks.values(), return_exceptions=True)
    assert j.id in await store.running_ids()
    w2 = make_worker(store)
    assert await w2.recover() == 1
    await w2.drain()
    done = await store.require(j.id)
    assert done.status == Status.DONE and done.attempts == 2


async def test_pipeline_draft_then_judge(store):
    async def ask(ctx: Context) -> Stages | Done:
        if ctx.job.parent is None:
            return Stages([{"kind": "ask", "payload": {"q": "draft"}}, {"kind": "judge"}])
        return Done({"answer": "draft text"})

    w = make_worker(store)
    w.handlers["ask"] = ask
    root = await submit(store, "ask")
    await w.drain()
    done = await store.require(root.id)
    assert done.status == Status.DONE
    assert done.result == {"answer": "draft text", "verdict": "pass", "judged": "draft text"}
    stages = await store.children(root.id)
    assert [s.kind for s in stages] == ["ask", "judge"]


async def test_mixed_load_minimizes_swaps_and_honours_priority(store):
    models = Models()
    w = make_worker(store, models=models)
    order: list[str] = []

    async def ask(ctx: Context) -> Done:
        order.append(ctx.job.payload["tag"])
        await asyncio.sleep(0)
        return Done({})

    w.handlers["ask"] = ask
    w.handlers["judge"] = ask
    # 30 jobs from three "devices": alternating models, mixed priorities.
    for i in range(30):
        kind = "ask" if i % 2 else "judge"
        prio = Priority.BATCH if i % 3 else Priority.INTERACTIVE
        await submit(store, kind, payload={"tag": f"{kind}-{i}-{int(prio)}"}, priority=prio)
    await w.drain()
    statuses = {(await store.require(j.id)).status for j in await store.history(100)}
    assert statuses == {Status.DONE}
    assert len(order) == 30
    # Interactive jobs (prio 0) all run before any batch job.
    first_batch = next(i for i, t in enumerate(order) if t.endswith("-2"))
    assert all(t.endswith("-0") for t in order[:first_batch])
    assert all(t.endswith("-2") for t in order[first_batch:])
    # Swaps stay near the optimum: 2 models x 2 priority classes -> at most 4 loads (+1 slack).
    assert len(models.loads) <= 5, models.loads


async def test_light_and_none_lanes_run_alongside_heavy(store):
    gate = asyncio.Event()
    concurrent: set[str] = set()
    peak = 0

    async def gated(ctx: Context) -> Done:
        nonlocal peak
        concurrent.add(ctx.job.id)
        peak = max(peak, len(concurrent))
        await gate.wait()
        concurrent.discard(ctx.job.id)
        return Done({})

    w = make_worker(store)
    for kind in ("slow", "tts", "search"):
        w.handlers[kind] = gated
    await submit(store, "slow")
    for _ in range(3):
        await submit(store, "tts")
    await submit(store, "search")
    await w.schedule_once()
    for _ in range(20):
        await asyncio.sleep(0)
    assert peak == 5  # 1 heavy + 3 light + 1 none, all at once
    gate.set()
    await w.drain()


async def _fast_sleep(_s: float) -> None:
    await _REAL_SLEEP(0)


_REAL_SLEEP = asyncio.sleep


async def test_cancel_interrupts_a_handler_with_no_checkpoints(store):
    started = asyncio.Event()

    async def blocking(ctx: Context) -> Done:
        started.set()
        await asyncio.sleep(3600)  # e.g. a long model call that never checks for cancellation
        return Done({})

    w = make_worker(store)
    w.handlers["slow"] = blocking
    j = await submit(store, "slow")
    await w.schedule_once()
    await asyncio.wait_for(started.wait(), 2)
    w.cancel_events[j.id].set()
    await w.drain(timeout_s=5)
    assert (await store.require(j.id)).status == Status.CANCELLED


async def test_recover_does_not_revive_cancelled_jobs(store):
    w1 = make_worker(store)
    j = await submit(store, "slow")
    await w1.schedule_once()
    await store.request_cancel(j.id)  # cancel requested while running...
    for t in w1.tasks.values():  # ...then the worker dies before acting on it
        t.cancel()
    await asyncio.gather(*w1.tasks.values(), return_exceptions=True)
    w2 = make_worker(store)
    await w2.recover()
    assert (await store.require(j.id)).status == Status.CANCELLED
    assert await store.queued() == []


async def test_cancel_pipeline_cancels_its_running_stage(store):
    root = await store.submit(Job(kind="task"))
    await store.mark_waiting(root)
    stage = await store.submit(Job(kind="slow", parent=root.id))
    assert await store.claim(stage)
    await store.request_cancel(root.id)
    assert (await store.require(root.id)).status == Status.CANCELLED
    assert (await store.require(stage.id)).cancel_requested


async def test_planned_restart_requeues_without_using_a_retry():
    from law.queue.worker import Worker

    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    started = asyncio.Event()

    async def slow(ctx):  # type: ignore[no-untyped-def]
        started.set()
        await asyncio.sleep(60)

    w = Worker(store, {"slow": slow}, Capacity(budget_gb=48), lambda j: j)
    job = await store.submit(Job(kind="slow", payload={}, lane="none"))
    runner = asyncio.create_task(w.run())
    await asyncio.wait_for(started.wait(), 5)
    w.stop()
    await asyncio.wait_for(runner, 5)
    again = await store.require(job.id)
    assert again.status == Status.QUEUED and again.attempts == 0  # the attempt was given back


async def test_dead_stage_found_on_startup_fails_its_pipeline_and_can_be_retried():
    from law.queue.worker import Worker, retry_pipeline

    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    calls: list[str] = []

    async def first(ctx):  # type: ignore[no-untyped-def]
        return Done({"brief": "b"})

    async def second(ctx):  # type: ignore[no-untyped-def]
        calls.append(ctx.job.payload["_carry"]["brief"])
        return Done({"ok": True})

    async def root(ctx):  # type: ignore[no-untyped-def]
        return Stages([{"kind": "first"}, {"kind": "second"}])

    handlers = {"root": root, "first": first, "second": second}
    w = Worker(store, handlers, Capacity(budget_gb=48), lambda j: j)
    top = await store.submit(Job(kind="root", payload={}, lane="none"))
    await w.schedule_once()  # root starts the pipeline
    await asyncio.gather(*w.tasks.values())
    await w.schedule_once()  # stage 0
    await asyncio.gather(*w.tasks.values())
    stage1 = next(c for c in await store.children(top.id) if c.stage == 1)
    assert await store.claim(stage1)  # a worker took stage 1 ...
    stage1 = await store.require(stage1.id)
    stage1.attempts = stage1.max_attempts  # ... and died with it, for the last time
    await store.save(stage1)
    w2 = Worker(store, handlers, Capacity(budget_gb=48), lambda j: j)
    await w2.recover()
    assert (await store.require(top.id)).status == Status.FAILED  # not stuck in waiting
    retried = await retry_pipeline(store, lambda j: j, top.id)
    assert retried.stage == 1 and (await store.require(top.id)).status == Status.WAITING
    await w2.drain()
    assert calls == ["b"]  # the earlier stage's result was kept
    assert (await store.require(top.id)).status == Status.DONE
    with pytest.raises(ValueError, match="only a failed, cancelled or stuck"):
        await retry_pipeline(store, lambda j: j, top.id)


async def test_skip_continues_after_the_failed_stage_and_single_jobs_retry_as_new_jobs():
    from law.queue.worker import Worker, retry_job, skip_stage

    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    ran: list[str] = []

    async def boom(ctx):  # type: ignore[no-untyped-def]
        raise Fatal("render failed")

    async def last(ctx):  # type: ignore[no-untyped-def]
        ran.append("last")
        return Done({"mailed": True})

    async def root(ctx):  # type: ignore[no-untyped-def]
        return Stages([{"kind": "boom"}, {"kind": "last"}])

    w = Worker(store, {"root": root, "boom": boom, "last": last}, Capacity(budget_gb=48), lambda j: j)
    top = await store.submit(Job(kind="root", payload={}, lane="none"))
    await w.drain()
    assert (await store.require(top.id)).status == Status.FAILED
    await skip_stage(store, lambda j: j, top.id)
    await w.drain()
    done = await store.require(top.id)
    assert ran == ["last"] and done.status == Status.DONE and done.payload["_runs"] == 1

    single = await store.submit(Job(kind="boom", payload={"prompt": "x"}, lane="none"))
    await w.drain()
    again = await retry_job(store, lambda j: j, single.id)
    assert again.id != single.id and again.payload == {"prompt": "x"} and again.status == Status.QUEUED
    with pytest.raises(ValueError, match="nothing to skip"):
        await skip_stage(store, lambda j: j, single.id)


async def test_worker_restarts_on_new_code_only_between_steps(tmp_path):
    from law.queue.worker import Worker

    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    flag = tmp_path / "worker-restart.pending"
    release = asyncio.Event()

    async def step(ctx):  # type: ignore[no-untyped-def]
        await release.wait()
        return Done({})

    w = Worker(store, {"step": step}, Capacity(budget_gb=48), lambda j: j, restart_flag=flag, tick_s=0.02)
    job = await store.submit(Job(kind="step", payload={}, lane="none"))
    runner = asyncio.create_task(w.run())
    await asyncio.sleep(0.2)
    flag.write_text("new")
    await asyncio.sleep(0.2)
    assert not runner.done()  # a step is running: no restart yet
    release.set()
    await asyncio.wait_for(runner, 5)  # finished the step, then exited for the restart
    assert (await store.require(job.id)).status == Status.DONE and not flag.exists()


async def test_pending_restart_takes_no_new_step_even_when_the_next_one_could_start_at_once(tmp_path):
    from law.queue.worker import Worker

    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    flag = tmp_path / "worker-restart.pending"
    ran: list[str] = []
    gate = asyncio.Event()

    async def first(ctx):  # type: ignore[no-untyped-def]
        ran.append("first")
        await gate.wait()
        return Done({"a": 1})

    async def second(ctx):  # type: ignore[no-untyped-def]
        ran.append("second")
        return Done({})

    async def root(ctx):  # type: ignore[no-untyped-def]
        return Stages([{"kind": "first"}, {"kind": "second"}])

    def on_model(job: Job) -> Job:  # the steps are agent steps on a model, as in a real pipeline
        if job.kind in {"first", "second"}:
            job.lane, job.resource, job.mem_gb = "heavy", "llm:m", 30
        return job

    w = Worker(store, {"root": root, "first": first, "second": second}, Capacity(budget_gb=48), on_model,
               restart_flag=flag, tick_s=0.02)
    top = await store.submit(Job(kind="root", payload={}, lane="none"))
    runner = asyncio.create_task(w.run())
    for _ in range(100):
        if ran:
            break
        await asyncio.sleep(0.02)
    flag.write_text("new")
    gate.set()
    await asyncio.wait_for(runner, 5)  # finished 'first', then restarted without starting 'second'
    assert ran == ["first"] and not flag.exists()
    second_stage = next(c for c in await store.children(top.id) if c.stage == 1)
    assert second_stage.status == Status.QUEUED  # waiting for the restarted worker


async def test_a_pending_restart_still_runs_quick_jobs_but_no_model_steps(tmp_path):
    from law.queue.worker import Worker

    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    flag = tmp_path / "worker-restart.pending"
    flag.write_text("new")
    ran: list[str] = []

    async def mail(ctx):  # type: ignore[no-untyped-def]
        ran.append("mail")
        return Done({})

    async def agent(ctx):  # type: ignore[no-untyped-def]
        ran.append("agent")
        return Done({})

    w = Worker(store, {"mail": mail, "agent": agent}, Capacity(budget_gb=48), lambda j: j, restart_flag=flag)
    await store.submit(Job(kind="agent", payload={}, lane="heavy", resource="llm:m", mem_gb=30))
    await store.submit(Job(kind="mail", payload={}, lane="none"))
    await w.schedule_once(quick_only=True)
    await asyncio.gather(*w.tasks.values())
    assert ran == ["mail"]  # the email went out; the agent step waits for the restarted worker


async def test_a_chat_runs_beside_a_long_agent_step_on_the_loaded_model(store):
    release = asyncio.Event()
    ran_on: dict[str, str] = {}

    async def slow(ctx: Context) -> Done:
        await release.wait()
        return Done({"ok": True})

    async def ask(ctx: Context) -> Done:
        ran_on[ctx.job.id] = ctx.job.resource
        return Done({"answer": "hi"})

    models = Models()
    w = make_worker(store, handlers={"slow": slow, "ask": ask}, models=models)
    agent = await store.submit(resolve(Job(kind="slow", payload={})))
    await w.schedule_once()
    asked = resolve(Job(kind="ask", payload={"share_model": True}))
    asked.resource = "llm:fast"
    await store.submit(asked)
    for _ in range(50):
        await w.schedule_once()
        if (await store.require(asked.id)).status == Status.DONE:
            break
        await asyncio.sleep(0.01)
    assert (await store.require(asked.id)).status == Status.DONE  # answered while the agent step still runs
    assert ran_on[asked.id] == "llm:a" and models.loads == ["llm:a"]  # on the loaded model: no swap
    assert (await store.require(agent.id)).status == Status.RUNNING
    release.set()
    await w.drain()


async def test_a_sharing_job_keeps_the_model_after_the_step_it_joined_ends(store):
    agent_done, chat_done = asyncio.Event(), asyncio.Event()

    async def slow(ctx: Context) -> Done:
        await agent_done.wait()
        return Done({"ok": True})

    async def ask(ctx: Context) -> Done:
        await chat_done.wait()
        return Done({"answer": "hi"})

    w = make_worker(store, handlers={"slow": slow, "ask": ask}, models=Models())
    await store.submit(resolve(Job(kind="slow", payload={})))
    await w.schedule_once()
    asked = await store.submit(resolve(Job(kind="ask", payload={"share_model": True})))
    await w.schedule_once()
    agent_done.set()
    for _ in range(50):
        if w.running.heavy is not None and w.running.heavy.id == asked.id:
            break
        await asyncio.sleep(0.01)
    assert w.running.heavy is not None and w.running.heavy.id == asked.id and w.running.shared == []
    chat_done.set()
    await w.drain()


async def test_pipeline_repeats_a_stage_that_has_work_left(store):
    runs: list[int] = []

    async def ask(ctx: Context) -> Stages | Done:
        if ctx.job.parent is None:
            return Stages([{"kind": "ask", "payload": {"q": "build"}}, {"kind": "judge"}])
        runs.append(ctx.job.stage)
        return Done({"answer": "built", "_again": len(runs) < 3})

    w = make_worker(store)
    w.handlers["ask"] = ask
    root = await submit(store, "ask")
    await w.drain()
    done = await store.require(root.id)
    assert done.status == Status.DONE and "_again" not in done.result
    assert runs == [0, 1, 2]
    assert [s.kind for s in await store.children(root.id)] == ["ask", "ask", "ask", "judge"]


async def test_pipeline_does_not_add_a_stage_when_a_repeat_follows(store):
    async def ask(ctx: Context) -> Stages | Done:
        if ctx.job.parent is None:
            return Stages([{"kind": "ask", "payload": {"q": "build"}}] * 2)
        return Done({"_again": ctx.job.stage == 0})

    w = make_worker(store)
    w.handlers["ask"] = ask
    root = await submit(store, "ask")
    await w.drain()
    assert len(await store.children(root.id)) == 2


async def test_pipeline_repeats_stop_at_the_stage_cap(store, monkeypatch):
    monkeypatch.setattr("law.queue.worker.MAX_STAGES", 4)

    async def ask(ctx: Context) -> Stages | Done:
        if ctx.job.parent is None:
            return Stages([{"kind": "ask", "payload": {"q": "build"}}])
        return Done({"_again": True})

    w = make_worker(store)
    w.handlers["ask"] = ask
    root = await submit(store, "ask")
    await w.drain()
    assert (await store.require(root.id)).status == Status.DONE
    assert len(await store.children(root.id)) == 4
