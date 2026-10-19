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
