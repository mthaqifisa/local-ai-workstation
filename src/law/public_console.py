"""Read-only Console for forwarding to the internet (ADR-0022).

The Console's API has no login and can cancel jobs, change settings and reset the machine, so it must never be
forwarded. This separate service binds to loopback and has only the GET routes below. Nothing here can change LAW:
it holds no job store, no write route and no secrets it serves, and it reads the API over loopback.

Every response is rebuilt from an allowlist, so a field the Console gains later stays private until it is listed
here. The owner's words stay home: chat text, job prompts, ticket details, notes, logs, errors, agent actions and
folders are never sent. Only status, numbers and ticket titles are. What is left then goes through `redact`.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from law import registry, secrets
from law.paths import LawPaths

STATIC = Path(__file__).parent / "static"
LOOPBACK = "127.0.0.1"  # the port forward reaches this; the LAN and the internet cannot reach it directly
CACHE_S = 3.0  # a flood of requests costs one call to the API per route in this time
CACHE_ENTRIES = 64
BACKEND_CALLS = 4
PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.IGNORECASE)
TOKENS = re.compile(r"\b(?:\d{8,10}:[\w-]{30,}|gh[pousr]_\w{20,}|sk-[\w-]{20,}|hf_\w{20,}|xox[a-z]-[\w-]{10,}"
                    r"|AKIA[0-9A-Z]{16}|Bearer\s+[\w.~+/-]{16,})")
EMAILS = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}
SYSTEM = ("cpu_percent", "cpu_cores", "memory", "disk", "gpu_percent", "loaded_models")
HEALTH = ("ok", "valkey", "queued", "running")
MODEL = ("active", "loaded", "last", "memory")
AGENT = ("id", "kind", "source", "status", "health", "created", "last_write", "last_10min")
STAGE = ("index", "total", "kind", "role", "status", "started")
JOB = ("id", "kind", "source", "status", "created", "parent")
PROJECT = ("id", "name", "state", "version", "stack", "live")
TICKET = ("id", "title", "kind", "epic", "column", "state", "started", "finished")
STEP = ("stage", "kind", "role", "column", "state")
Fetch = Callable[[str], Awaitable[Any]]


def pick(data: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any]:
    return {k: data[k] for k in fields if k in data} if data else {}


def redact(value: Any, secret_values: set[str], home: str) -> Any:
    """A last net under the allowlists: secrets, anything token-shaped, email addresses and the home folder."""
    if isinstance(value, dict):
        return {k: redact(v, secret_values, home) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret_values, home) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secret_values:
        value = value.replace(secret, "[hidden]")
    value = EMAILS.sub("[hidden]", TOKENS.sub("[hidden]", value))
    return value.replace(home, "~") if home else value


def public_agents(data: dict[str, Any]) -> dict[str, Any]:
    jobs = []
    for job in data.get("jobs", []):
        plan = job.get("plan")
        folder = str(job.get("project") or "")
        jobs.append({**pick(job, AGENT), "title": Path(folder).name or str(job.get("kind", "job")), "error": None,
                     "error_line": None, "stage": pick(job.get("stage"), STAGE) or None, "last_action": None,
                     "recent": [], "project": None, "actions": [],
                     "plan": {"done": plan.get("done"), "total": plan.get("total"), "next": plan.get("next")}
                     if plan else None})
    return {"model": pick(data.get("model"), MODEL), "jobs": jobs}


def public_jobs(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**pick(job, JOB), "payload": {}, "error": None} for job in data]


def public_board(data: dict[str, Any]) -> dict[str, Any]:
    tickets = [{**pick(t, TICKET), "details": "", "logs": [], "notes": [], "jobs": [],
                "steps": [pick(s, STEP) for s in t.get("steps", [])]} for t in data.get("tickets", [])]
    return {"project": {**pick(data.get("project"), PROJECT), "dir": "", "summary": "", "history": [], "approve": None},
            "columns": data.get("columns", []), "tickets": tickets, "notes": []}


def read_only_page() -> str:
    return (STATIC / "console.html").read_text().replace("<html", "<html data-readonly", 1)


def create_public_app(paths: LawPaths, fetch: Fetch | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    values = {v for v in secrets.read(paths.env_file).values() if len(v) >= 8}
    home = str(paths.user_home)
    api_port = registry.load(paths.registry_file).ports.law_api
    cache: dict[str, tuple[float, Any]] = {}
    calls = asyncio.Semaphore(BACKEND_CALLS)
    page = read_only_page()

    async def from_api(path: str) -> Any:
        async with httpx.AsyncClient(base_url=f"http://{LOOPBACK}:{api_port}", timeout=20) as client:
            r = await client.get(path)
        if r.status_code == 404:
            raise HTTPException(404, "not found")
        r.raise_for_status()
        return r.json()

    get = fetch or from_api

    async def serve(path: str, clean: Callable[[Any], Any]) -> Any:
        hit = cache.get(path)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        async with calls:
            try:
                data = redact(clean(await get(path)), values, home)
            except httpx.HTTPError as exc:
                raise HTTPException(503, "unavailable") from exc
        if len(cache) >= CACHE_ENTRIES:
            cache.clear()
        cache[path] = (time.monotonic(), data)
        return data

    @app.middleware("http")
    async def harden(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    @app.get("/", response_class=HTMLResponse)
    @app.get("/console", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/healthz")
    async def health() -> Any:
        return await serve("/healthz", lambda d: pick(d, HEALTH))

    @app.get("/system")
    async def system() -> Any:
        return await serve("/system", lambda d: pick(d, SYSTEM))

    @app.get("/agents")
    async def agents() -> Any:
        return await serve("/agents", public_agents)

    @app.get("/jobs")
    async def jobs() -> Any:  # a fixed limit: a request cannot ask for more
        return await serve("/jobs?limit=120", public_jobs)

    @app.get("/board")
    async def boards() -> Any:
        return await serve("/board", lambda rows: [pick(p, ("id", "name", "state", "version")) for p in rows])

    @app.get("/board/{pid}")
    async def board(pid: str) -> Any:
        if not PROJECT_ID.fullmatch(pid):
            raise HTTPException(404, "not found")
        return await serve(f"/board/{pid}", public_board)

    return app


def serve_public(paths: LawPaths) -> None:
    import uvicorn

    port = registry.load(paths.registry_file).ports.law_public
    uvicorn.run(create_public_app(paths), host=LOOPBACK, port=port, log_level="warning", server_header=False,
                date_header=False, proxy_headers=False)

