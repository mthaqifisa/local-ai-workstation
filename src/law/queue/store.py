"""Valkey-backed durable job store.

Keys (all prefixed `law:`):
  job:<id>        JSON of the Job (source of truth for one job)
  queued          ZSET id -> enqueued time (jobs waiting to start)
  running         ZSET id -> lease expiry (crash recovery: expired leases are requeued)
  waiting         SET of pipeline roots waiting for stages
  dead            ZSET id -> time (exhausted retries)
  history         ZSET id -> created time (every job, for `law jobs`)
  events:<id>     LIST of progress events (replayable)
  wake            Pub/Sub channel nudging the worker
  resident        STRING: model currently held by the heavy lane
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as aioredis

from law.queue.jobs import Job, Status

P = "law:"

LEASE_S = 60.0
EVENT_KEEP = 500


class JobStore:
    def __init__(self, client: aioredis.Redis) -> None:
        self.r = client

    @classmethod
    def connect(cls, url: str) -> JobStore:
        return cls(aioredis.from_url(url, decode_responses=True))

    async def close(self) -> None:
        await self.r.aclose()

    # ---- basic CRUD -------------------------------------------------------
    async def save(self, job: Job) -> None:
        await self.r.set(f"{P}job:{job.id}", job.model_dump_json())

    async def get(self, job_id: str) -> Job | None:
        raw = await self.r.get(f"{P}job:{job_id}")
        return Job.model_validate_json(raw) if raw else None

    async def require(self, job_id: str) -> Job:
        job = await self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    # ---- lifecycle -------------------------------------------------------
    async def submit(self, job: Job) -> Job:
        job.status = Status.QUEUED
        job.enqueued = time.time()
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(f"{P}job:{job.id}", job.model_dump_json())
            pipe.zadd(f"{P}queued", {job.id: job.enqueued})
            pipe.zadd(f"{P}history", {job.id: job.created})
            await pipe.execute()
        await self.event(job.id, "queued", {"kind": job.kind, "priority": int(job.priority)})
        await self.r.publish(f"{P}wake", job.id)
        return job

    async def queued(self) -> list[Job]:
        ids = await self.r.zrange(f"{P}queued", 0, -1)
        jobs = [await self.get(str(i)) for i in ids]
        return [j for j in jobs if j is not None]

    async def running_ids(self) -> list[str]:
        return [str(x) for x in await self.r.zrange(f"{P}running", 0, -1)]

    async def claim(self, job: Job) -> bool:
        """Move a job from queued to running. False if another claimer won or it vanished."""
        removed = await self.r.zrem(f"{P}queued", job.id)
        if not removed:
            return False
        job.status = Status.RUNNING
        job.started = time.time()
        job.attempts += 1
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(f"{P}job:{job.id}", job.model_dump_json())
            pipe.zadd(f"{P}running", {job.id: time.time() + LEASE_S})
            await pipe.execute()
        await self.event(job.id, "started", {"attempt": job.attempts})
        return True

    async def heartbeat(self, job_id: str) -> None:
        await self.r.zadd(f"{P}running", {job_id: time.time() + LEASE_S}, xx=True)

    async def finish(self, job: Job, status: Status, *, result: dict[str, Any] | None = None,
                     error: str | None = None) -> None:
        job.status = status
        job.finished = time.time()
        job.result = result
        job.error = error
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(f"{P}job:{job.id}", job.model_dump_json())
            pipe.zrem(f"{P}running", job.id)
            pipe.zrem(f"{P}queued", job.id)
            pipe.srem(f"{P}waiting", job.id)
            if status == Status.DEAD:
                pipe.zadd(f"{P}dead", {job.id: time.time()})
            await pipe.execute()
        await self.event(job.id, status.value, {"error": error} if error else (result or {}))
        await self.r.publish(f"{P}wake", job.id)

    async def requeue(self, job: Job, reason: str) -> None:
        """Return a running job to the queue (crash, retryable error). Keeps its original age."""
        job.status = Status.QUEUED
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(f"{P}job:{job.id}", job.model_dump_json())
            pipe.zrem(f"{P}running", job.id)
            pipe.zadd(f"{P}queued", {job.id: job.enqueued})
            await pipe.execute()
        await self.event(job.id, "requeued", {"reason": reason})
        await self.r.publish(f"{P}wake", job.id)

    async def mark_waiting(self, job: Job) -> None:
        job.status = Status.WAITING
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.set(f"{P}job:{job.id}", job.model_dump_json())
            pipe.zrem(f"{P}running", job.id)
            pipe.sadd(f"{P}waiting", job.id)
            await pipe.execute()
        await self.event(job.id, "waiting", {})

    async def expired_leases(self, now: float | None = None) -> list[str]:
        return [str(x) for x in await self.r.zrangebyscore(f"{P}running", "-inf", now or time.time())]

    async def request_cancel(self, job_id: str) -> Job:
        job = await self.require(job_id)
        if job.status.terminal:
            return job
        job.cancel_requested = True
        await self.save(job)
        if job.status is Status.WAITING:  # a pipeline: stop the stage that is running for it
            for child in await self.children(job.id):
                await self.request_cancel(child.id)
        if job.status in {Status.QUEUED, Status.WAITING}:
            await self.finish(job, Status.CANCELLED, error="cancelled by user")
        else:
            await self.r.publish(f"{P}cancel", job_id)
        return job

    # ---- events ----------------------------------------------------------
    async def event(self, job_id: str, kind: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"t": time.time(), "job": job_id, "event": kind, "data": data})
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.rpush(f"{P}events:{job_id}", payload)
            pipe.ltrim(f"{P}events:{job_id}", -EVENT_KEEP, -1)
            pipe.publish(f"{P}events", payload)
            await pipe.execute()

    async def events(self, job_id: str) -> list[dict[str, Any]]:
        return [json.loads(e) for e in await self.r.lrange(f"{P}events:{job_id}", 0, -1)]

    async def follow(self, job_id: str, poll_s: float = 0.5) -> AsyncIterator[dict[str, Any]]:
        """Yield every event of a job (history first), until the job is terminal."""
        seen = 0
        while True:
            items = await self.r.lrange(f"{P}events:{job_id}", seen, -1)
            for raw in items:
                seen += 1
                yield json.loads(raw)
            job = await self.get(job_id)
            if job is None or (job.status.terminal and not items):
                return
            await asyncio.sleep(poll_s)

    # ---- resident model & history ---------------------------------------
    async def set_resident(self, resource: str | None) -> None:
        if resource:
            await self.r.set(f"{P}resident", resource)
        else:
            await self.r.delete(f"{P}resident")

    async def resident(self) -> str | None:
        value = await self.r.get(f"{P}resident")
        return str(value) if value else None

    async def history(self, limit: int = 20) -> list[Job]:
        ids = await self.r.zrevrange(f"{P}history", 0, limit - 1)
        jobs = [await self.get(str(i)) for i in ids]
        return [j for j in jobs if j is not None]

    async def prune(self, older_than: float, keep: frozenset[str] = frozenset()) -> int:
        """Delete finished jobs created before `older_than` with their progress logs. Live jobs, the stages of
        a pipeline that is still live (a retry needs them), and the pipelines in `keep` with their stages are kept.
        Returns how many were deleted."""
        deleted = 0
        for raw in await self.r.zrangebyscore(f"{P}history", 0, older_than):
            job_id = str(raw)
            job = await self.get(job_id)
            if job is not None and not job.status.terminal:
                continue
            if job_id in keep or (job is not None and job.parent in keep):
                continue
            if job is not None and job.parent:
                root = await self.get(job.parent)
                if root is not None and not root.status.terminal:
                    continue
            async with self.r.pipeline(transaction=True) as pipe:
                pipe.delete(f"{P}job:{job_id}", f"{P}events:{job_id}")
                pipe.zrem(f"{P}history", job_id)
                pipe.zrem(f"{P}dead", job_id)
                await pipe.execute()
            deleted += 1
        return deleted

    async def children(self, root_id: str) -> list[Job]:
        out = []
        for job in await self.history(limit=1000):
            if job.parent == root_id:
                out.append(job)
        return sorted(out, key=lambda j: j.stage)

