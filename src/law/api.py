"""The `law` job API (`law serve`): the network front end of the job queue (ADR-0003).

One small FastAPI service, bound to `0.0.0.0:<law_api>` with no auth (user decision,
ADR-0006: the Mac is the household AI server on a trusted LAN). The CLI, the Telegram
adapter and the Homepage dashboard all speak to this one service, so a job behaves the
same whatever submitted it.

It does not run jobs — the worker (`law worker`) does. The API only:
  * validates and resolves an incoming job (fills resource/lane/mem_gb from the registry),
  * writes it to the shared Valkey store,
  * reads back status, events, results and pipeline children,
  * streams live progress as Server-Sent Events,
  * requests cancellation,
  * serves generated output files back to clients.

Both processes share the same `JobStore`, so submitting here and executing in the worker
need no other channel than Valkey.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from law import registry
from law.handlers.base import Runtime
from law.paths import LawPaths
from law.queue.jobs import Job, Priority, Status
from law.queue.store import JobStore
from law.queue.worker import Resolver

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SubmitRequest(BaseModel):
    """What a client posts to start a job. Resource needs are filled in server-side."""

    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: Priority = Priority.INTERACTIVE
    source: str = "api"
    reply_to: dict[str, Any] | None = None


class CancelResponse(BaseModel):
    id: str
    status: str
    cancel_requested: bool


class Health(BaseModel):
    ok: bool
    valkey: bool
    queued: int
    running: int


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def redis_url(reg: registry.Registry) -> str:
    """Where the store lives. `LAW_REDIS_URL` overrides it (tests, non-default hosts)."""
    override = os.environ.get("LAW_REDIS_URL")
    if override:
        return override
    return f"redis://{reg.network.loopback}:{reg.ports.valkey}/0"


def _store(request: Request) -> JobStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _resolve(request: Request) -> Resolver:
    return request.app.state.resolve  # type: ignore[no-any-return]


# FastAPI injection via Annotated (keeps `Depends(...)` out of argument defaults).
StoreDep = Annotated[JobStore, Depends(_store)]
ResolveDep = Annotated[Resolver, Depends(_resolve)]


async def _require(store: JobStore, job_id: str) -> Job:
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job {job_id!r}")
    return job


def create_app(
    paths: LawPaths | None = None,
    *,
    store: JobStore | None = None,
    resolve: Resolver | None = None,
) -> FastAPI:
    """Build the API.

    In production nothing is passed: the app connects to Valkey and builds the registry
    resolver on startup from `paths`. Tests inject a fakeredis-backed `store` and a trivial
    `resolve`, so the HTTP layer can be exercised without a running machine.
    """

    paths = paths or LawPaths.from_env()
    reg = registry.load(paths.registry_file)
    injected = store is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = store or JobStore.connect(redis_url(reg))
        if resolve is not None:
            app.state.resolve = resolve
        else:
            from law import app as wiring

            app.state.resolve = wiring.resolver(Runtime.load(paths))
        try:
            yield
        finally:
            if not injected:
                await app.state.store.close()

    app = FastAPI(title="law", version="6.0.0", lifespan=lifespan)
    app.state.outputs = paths.outputs
    from law import console

    console.mount(app, f"http://{reg.network.loopback}:{reg.ports.llm_internal}", paths)

    # ---- submit & inspect -------------------------------------------------
    @app.post("/jobs", response_model=Job, status_code=201)
    async def submit_job(req: SubmitRequest, store: StoreDep, resolve: ResolveDep) -> Job:
        job = Job(kind=req.kind, payload=req.payload, priority=req.priority,
                  source=req.source, reply_to=req.reply_to)
        try:
            job = resolve(job)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=f"cannot schedule {req.kind!r}: {exc}") from exc
        return await store.submit(job)

    @app.get("/jobs", response_model=list[Job])
    async def list_jobs(store: StoreDep, limit: Annotated[int, Query(ge=1, le=1000)] = 20) -> list[Job]:
        return await store.history(limit=limit)

    @app.get("/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: str, store: StoreDep) -> Job:
        return await _require(store, job_id)

    @app.get("/jobs/{job_id}/children", response_model=list[Job])
    async def job_children(job_id: str, store: StoreDep) -> list[Job]:
        await _require(store, job_id)
        return await store.children(job_id)

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, store: StoreDep) -> list[dict[str, Any]]:
        await _require(store, job_id)
        return await store.events(job_id)

    # ---- live progress ----------------------------------------------------
    @app.get("/jobs/{job_id}/stream")
    async def stream_job(job_id: str, store: StoreDep) -> EventSourceResponse:
        await _require(store, job_id)

        async def events() -> AsyncIterator[dict[str, str]]:
            async for event in store.follow(job_id):
                yield {"event": str(event.get("event", "progress")), "data": json.dumps(event)}

        return EventSourceResponse(events())

    # ---- control ----------------------------------------------------------
    @app.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
    async def cancel_job(job_id: str, store: StoreDep) -> CancelResponse:
        await _require(store, job_id)
        job = await store.request_cancel(job_id)
        return CancelResponse(id=job.id, status=job.status.value, cancel_requested=job.cancel_requested)

    @app.post("/jobs/{job_id}/retry", response_model=Job)
    async def retry(job_id: str, store: StoreDep, resolve: ResolveDep) -> Job:
        """Run a failed, cancelled or stuck job again: a pipeline from its stopped stage (earlier results kept),
        a single job as a new job. Returns the job that now runs."""
        from law.queue.worker import retry_job

        await _require(store, job_id)
        try:
            return await retry_job(store, resolve, job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/jobs/{job_id}/skip", response_model=Job)
    async def skip(job_id: str, store: StoreDep, resolve: ResolveDep) -> Job:
        """Continue a stopped pipeline after its failed stage."""
        from law.queue.worker import skip_stage

        await _require(store, job_id)
        try:
            return await skip_stage(store, resolve, job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/jobs/{job_id}/restart", response_model=Job)
    async def restart(job_id: str, store: StoreDep, resolve: ResolveDep) -> Job:
        """A stuck pipeline: stop the running stage, then run it again (its agent continues from .law/)."""
        from law.queue.worker import retry_job

        await _require(store, job_id)
        children = await store.children(job_id)
        live = [c for c in children if c.status in {Status.QUEUED, Status.RUNNING}]
        for child in live:
            await store.request_cancel(child.id)
        for _ in range(100):  # the worker stops the agent within seconds
            states = [(await store.require(c.id)).status for c in live]
            if all(st.terminal for st in states):
                break
            await asyncio.sleep(0.2)
        try:
            return await retry_job(store, resolve, job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ---- results & files --------------------------------------------------
    # Sync route: FastAPI runs it in a threadpool, so the blocking filesystem probes are fine.
    @app.get("/files")
    def get_file(request: Request, path: Annotated[str, Query(min_length=1)]) -> FileResponse:
        """Serve a generated artifact. Confined to `~/LAW/outputs` to stop path traversal."""
        outputs: Path = request.app.state.outputs
        try:
            target = (outputs / path).resolve()
            target.relative_to(outputs.resolve())
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail="path escapes the outputs directory") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        return FileResponse(target)

    @app.get("/healthz", response_model=Health)
    async def healthz(store: StoreDep) -> Health:
        try:
            alive = bool(await store.r.ping())
        except Exception:  # any connection error means unhealthy; never surface a 500 from a health probe
            return Health(ok=False, valkey=False, queued=0, running=0)
        queued = len(await store.queued())
        running = len(await store.running_ids())
        return Health(ok=True, valkey=alive, queued=queued, running=running)

    return app


def serve(paths: LawPaths | None = None, *, host: str | None = None, port: int | None = None) -> None:
    """Run the API with uvicorn. Called by `law serve` and by `python -m law.api`."""
    import uvicorn

    paths = paths or LawPaths.from_env()
    reg = registry.load(paths.registry_file)
    uvicorn.run(
        create_app(paths),
        host=host or reg.network.bind,
        port=port or reg.ports.law_api,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    serve()
