"""The LAW worker: pulls jobs from Valkey, admits them through the scheduler, runs handlers.

One worker process owns execution (heavy lane = one job). Crash safety comes from leases:
anything left in `running` by a dead worker is requeued on startup, keeping its original age.
Pipelines (draft -> judge -> revise ...) are chains of stage jobs so a stage never holds the heavy
lane while waiting for another stage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from law.queue.jobs import Job, Priority, Status
from law.queue.scheduler import Capacity, Running, pick
from law.queue.store import JobStore

log = logging.getLogger("law.worker")


class Fatal(Exception):
    """A job error that retrying cannot fix (bad input, missing file)."""


class Cancelled(Exception):
    """Raised inside a handler when the user cancelled the job."""


@dataclass
class Done:
    result: dict[str, Any]


@dataclass
class Stages:
    """Continue as a pipeline: run these job specs one after another, then finish the root."""

    specs: list[dict[str, Any]]
    carry: dict[str, Any] = field(default_factory=dict)


Outcome = Done | Stages


class ModelControl(Protocol):
    async def prepare(self, resource: str, resident: str | None) -> None:
        """Make memory available for `resource` (e.g. unload the resident LLM before a media job)."""


class NoModelControl:
    async def prepare(self, resource: str, resident: str | None) -> None:
        return None


@dataclass
class Context:
    """What a handler receives besides its job."""

    store: JobStore
    job: Job
    cancel_event: asyncio.Event

    async def progress(self, message: str, **data: Any) -> None:
        await self.store.event(self.job.id, "progress", {"message": message, **data})
        if self.job.parent:  # pipeline stages report on the job the user is watching too
            await self.store.event(self.job.parent, "progress", {"message": message, "stage": self.job.kind, **data})

    async def thought(self, text: str) -> None:
        """Live reasoning snippet (shown in Telegram while the model thinks)."""
        if text:
            await self.progress(f"💭 {text}", thought=True)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()


Handler = Callable[[Context], Awaitable[Outcome]]
Resolver = Callable[[Job], Job]  # fills resource / lane / mem_gb from the registry


async def submit_stage(store: JobStore, resolve: Callable[[Job], Job], root: Job, index: int,
                       carry: dict[str, Any]) -> Job:
    spec = root.payload["_stages"][index]
    stage = Job(kind=spec["kind"], payload={**spec.get("payload", {}), "_carry": carry},
                priority=Priority(spec.get("priority", int(root.priority))), source=root.source,
                parent=root.id, stage=index, reply_to=None)
    stage.created = root.created
    stage = resolve(stage)
    await store.submit(stage)
    total = len(root.payload["_stages"])
    await store.event(root.id, "progress",
                      {"message": f"stage {index + 1}/{total}: {stage.kind}", "stage_job": stage.id})
    return stage


async def retry_pipeline(store: JobStore, resolve: Callable[[Job], Job], root_id: str) -> Job:
    """Run a failed (or stuck) pipeline again from the stage that failed, keeping everything earlier stages
    produced. Raises ValueError if there is nothing to retry."""
    root = await store.get(root_id)
    if root is None or not root.payload.get("_stages"):
        raise ValueError(f"{root_id} is not a pipeline")
    if root.status in {Status.DONE, Status.CANCELLED} or root.status is Status.RUNNING:
        raise ValueError(f"{root_id} is {root.status.value}; only a failed or stuck pipeline can be retried")
    children = await store.children(root.id)
    if any(c.status in {Status.QUEUED, Status.RUNNING} for c in children):
        raise ValueError(f"{root_id} still has a stage in progress")
    failed = [c for c in children if c.status in {Status.FAILED, Status.DEAD}]
    if not failed:
        raise ValueError(f"{root_id} has no failed stage")
    index = max(c.stage or 0 for c in failed)
    root.error = None
    root.finished = None
    await store.mark_waiting(root)
    await store.event(root.id, "progress", {"message": f"retrying from stage {index + 1}"})
    return await submit_stage(store, resolve, root, index, root.payload.get("_carry", {}))


class Worker:
    def __init__(self, store: JobStore, handlers: dict[str, Handler], capacity: Capacity,
                 resolve: Resolver, models: ModelControl | None = None, tick_s: float = 0.5,
                 job_timeout_s: float = 6 * 3600) -> None:
        self.store = store
        self.handlers = handlers
        self.cap = capacity
        self.resolve = resolve
        self.models = models or NoModelControl()
        self.tick_s = tick_s
        self.job_timeout_s = job_timeout_s
        self.running = Running()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.swaps = 0

    # ---- public ------------------------------------------------------------
    async def recover(self) -> int:
        """Requeue jobs a previous worker left running. Returns how many."""
        n = 0
        for job_id in await self.store.running_ids():
            job = await self.store.get(job_id)
            if job is None:
                continue
            if job.cancel_requested:
                await self.store.finish(job, Status.CANCELLED, error="cancelled by user")
            elif job.attempts >= job.max_attempts:
                await self.store.finish(job, Status.DEAD, error="worker died; retries exhausted")
                await self._fail_parent(job, Status.FAILED, f"stage {job.stage} died: worker died; retries exhausted")
            else:
                await self.store.requeue(job, "worker restart")
            n += 1
        await self.store.set_resident(None)
        return n

    async def run(self) -> None:
        await self.recover()
        listener = asyncio.create_task(self._listen())
        try:
            while not self._stop.is_set():
                await self.schedule_once()
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.tick_s)
        finally:
            listener.cancel()
            await self._requeue_on_shutdown()
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _requeue_on_shutdown(self) -> None:
        """A planned stop (e.g. `law install` restarting the worker) is not a failure: give back the attempt and put
        each running job straight back in the queue, so restarts never use up a job's retries."""
        for job_id in list(self.tasks):
            job = await self.store.get(job_id)
            if job is None or job.status is not Status.RUNNING:
                continue
            job.attempts = max(0, job.attempts - 1)
            await self.store.requeue(job, "worker restart (planned)")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def schedule_once(self) -> list[str]:
        queued = await self.store.queued()
        resident = await self.store.resident()
        started: list[str] = []
        for job in pick(queued, self.running, resident, self.cap, time.time()):
            if await self.store.claim(job):
                self._start(job)
                started.append(job.id)
        return started

    async def drain(self, timeout_s: float = 120.0) -> None:
        """Run until no work is queued or running (used by tests and `law jobs --drain`)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await self.schedule_once()
            if not self.tasks and not await self.store.queued():
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("drain timed out")

    # ---- internals ------------------------------------------------------------
    def _start(self, job: Job) -> None:
        if job.lane == "heavy":
            self.running.heavy = job
        elif job.lane == "light":
            self.running.light.append(job)
        else:
            self.running.none.append(job)
        self.cancel_events[job.id] = asyncio.Event()
        self.tasks[job.id] = asyncio.create_task(self._execute(job))

    def _release(self, job: Job) -> None:
        if self.running.heavy and self.running.heavy.id == job.id:
            self.running.heavy = None
        self.running.light = [j for j in self.running.light if j.id != job.id]
        self.running.none = [j for j in self.running.none if j.id != job.id]
        self.tasks.pop(job.id, None)
        self.cancel_events.pop(job.id, None)
        self._wake.set()

    async def _heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(15)
            await self.store.heartbeat(job_id)

    async def _execute(self, job: Job) -> None:
        beat = asyncio.create_task(self._heartbeat(job.id))
        try:
            handler = self.handlers.get(job.kind)
            if handler is None:
                raise Fatal(f"no handler for job kind {job.kind!r}")
            if job.lane == "heavy" and job.resource:
                resident = await self.store.resident()
                if resident != job.resource:
                    await self.store.event(job.id, "progress", {"message": f"loading {job.resource}"})
                    await self.models.prepare(job.resource, resident)
                    await self.store.set_resident(job.resource)
                    self.swaps += 1
            ctx = Context(self.store, job, self.cancel_events[job.id])
            outcome = await self._run_cancellable(handler(ctx), self.cancel_events[job.id])
            if isinstance(outcome, Stages):
                await self._begin_pipeline(job, outcome)
            else:
                await self.store.finish(job, Status.DONE, result=outcome.result)
                await self._advance_parent(job, outcome.result)
        except Cancelled:
            await self.store.finish(job, Status.CANCELLED, error="cancelled by user")
            await self._fail_parent(job, Status.CANCELLED, "stage cancelled")
        except Fatal as exc:
            await self.store.finish(job, Status.FAILED, error=str(exc))
            await self._fail_parent(job, Status.FAILED, f"stage {job.stage} failed: {exc}")
        except Exception as exc:  # retryable: model crash, network, timeout
            log.warning("job %s attempt %s failed: %s", job.id, job.attempts, exc)
            detail = f"{type(exc).__name__}: {exc}"
            await self.store.event(job.id, "error", {"error": detail, "trace": traceback.format_exc()[-2000:]})
            if job.attempts >= job.max_attempts:
                await self.store.finish(job, Status.DEAD, error=detail)
                await self._fail_parent(job, Status.FAILED, f"stage {job.stage} exhausted retries: {detail}")
            else:
                await asyncio.sleep(min(30.0, 2.0 ** job.attempts))
                await self.store.requeue(job, detail)
        finally:
            beat.cancel()
            self._release(job)

    async def _run_cancellable(self, work: Awaitable[Outcome], cancel: asyncio.Event) -> Outcome:
        """Run a handler but stop it the moment the job is cancelled (a model call has no checkpoints)."""
        task = asyncio.ensure_future(work)
        stopper = asyncio.ensure_future(cancel.wait())
        try:
            done, _ = await asyncio.wait({task, stopper}, timeout=self.job_timeout_s,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if stopper in done:
                raise Cancelled()
            raise TimeoutError(f"job exceeded {self.job_timeout_s:.0f}s")
        finally:
            stopper.cancel()

    async def _begin_pipeline(self, root: Job, stages: Stages) -> None:
        root.payload = {**root.payload, "_stages": stages.specs, "_carry": stages.carry}
        await self.store.mark_waiting(root)
        await self._submit_stage(root, 0, stages.carry)

    async def _submit_stage(self, root: Job, index: int, carry: dict[str, Any]) -> None:
        await submit_stage(self.store, self.resolve, root, index, carry)

    async def _advance_parent(self, stage: Job, result: dict[str, Any]) -> None:
        if not stage.parent:
            return
        root = await self.store.get(stage.parent)
        if root is None or root.status.terminal:
            return
        carry = {**root.payload.get("_carry", {}), **result}
        root.payload = {**root.payload, "_carry": carry}
        await self.store.save(root)
        nxt = stage.stage + 1
        if nxt < len(root.payload["_stages"]):
            await self._submit_stage(root, nxt, carry)
        else:
            final = {k: v for k, v in carry.items() if not k.startswith("_")}
            await self.store.finish(root, Status.DONE, result=final)

    async def _fail_parent(self, stage: Job, status: Status, reason: str) -> None:
        if not stage.parent:
            return
        root = await self.store.get(stage.parent)
        if root and not root.status.terminal:
            await self.store.finish(root, status, error=reason)

    async def _listen(self) -> None:
        pubsub = self.store.r.pubsub()
        await pubsub.subscribe("law:wake", "law:cancel")
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                if msg["channel"] == "law:cancel":
                    ev = self.cancel_events.get(str(msg["data"]))
                    if ev:
                        ev.set()
                self._wake.set()
        finally:
            await pubsub.aclose()  # type: ignore[no-untyped-call]
