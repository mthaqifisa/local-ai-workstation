"""The worker keeps the Mac awake exactly while jobs run."""

from __future__ import annotations

import asyncio
import os
import sys

import fakeredis

from law.queue.awake import KeepAwake, caffeinate_argv
from law.queue.jobs import Job
from law.queue.scheduler import Capacity
from law.queue.store import JobStore
from law.queue.worker import Context, Done, Worker

HOLD = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_assertion_starts_once_and_stops():
    awake = KeepAwake(HOLD)
    awake.update(True)
    first = awake.proc
    awake.update(True)
    assert awake.holding and awake.proc is first
    awake.update(False)
    assert not awake.holding and first is not None and first.returncode is not None


def test_assertion_restarts_if_it_died_and_is_a_no_op_without_caffeinate():
    awake = KeepAwake(HOLD)
    awake.update(True)
    assert awake.proc is not None
    awake.proc.kill()
    awake.proc.wait()
    awake.update(True)
    assert awake.holding
    awake.update(False)
    none = KeepAwake(None)
    none.update(True)
    assert not none.holding


def test_caffeinate_command_is_tied_to_this_process():
    argv = caffeinate_argv()
    if argv is not None:
        assert argv[1:] == ["-i", "-s", "-w", str(os.getpid())]


async def test_worker_holds_the_assertion_only_while_a_job_runs():
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    awake = KeepAwake(HOLD)
    release = asyncio.Event()
    seen: list[bool] = []

    async def step(ctx: Context) -> Done:
        seen.append(awake.holding)
        await release.wait()
        return Done({})

    w = Worker(store, {"step": step}, Capacity(budget_gb=48), lambda j: j, awake=awake)
    assert not awake.holding
    await store.submit(Job(kind="step", payload={}, lane="none"))
    await w.schedule_once()
    await asyncio.sleep(0.05)
    assert seen == [True]
    release.set()
    await asyncio.gather(*w.tasks.values())
    assert not awake.holding
