"""HTTP client for the LAW job API that survives API restarts.

Jobs live in Valkey, not in the API process, so a restart of the API (or a dropped connection) must not
lose the result. `wait` streams progress; if the stream breaks it reconnects, falling back to polling.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

TERMINAL = {"done", "failed", "cancelled", "dead"}
EventHandler = Callable[[dict[str, Any]], None]


def submit(base: str, kind: str, payload: dict[str, Any], *, source: str = "cli", priority: int | None = None,
           retry_s: float = 120) -> dict[str, Any]:
    body: dict[str, Any] = {"kind": kind, "payload": payload, "source": source}
    if priority is not None:
        body["priority"] = priority
    deadline = time.monotonic() + retry_s
    while True:
        try:
            r = httpx.post(f"{base}/jobs", json=body, timeout=30)
            if r.status_code == 422:
                raise ValueError(r.json().get("detail", "invalid job"))
            r.raise_for_status()
            job: dict[str, Any] = r.json()
            return job
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout):
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def get(base: str, job_id: str) -> dict[str, Any] | None:
    try:
        r = httpx.get(f"{base}/jobs/{job_id}", timeout=15)
        r.raise_for_status()
        job: dict[str, Any] = r.json()
        return job
    except httpx.HTTPError:
        return None


def wait(base: str, job_id: str, on_event: EventHandler | None = None, *, timeout_s: float = 8 * 3600,
         poll_s: float = 3.0) -> dict[str, Any]:
    """Block until the job is terminal and return it. Reconnects across API restarts."""
    deadline = time.monotonic() + timeout_s
    seen = 0
    while time.monotonic() < deadline:
        try:
            with httpx.stream("GET", f"{base}/jobs/{job_id}/stream", timeout=httpx.Timeout(None, connect=10)) as s:
                count = 0
                for line in s.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    count += 1
                    if count > seen:  # replayed history after a reconnect is skipped
                        seen = count
                        if on_event:
                            on_event(json.loads(line[6:]))
        except httpx.HTTPError:
            pass  # API restarting or connection dropped; fall through to a status check
        job = get(base, job_id)
        if job and job["status"] in TERMINAL:
            return job
        time.sleep(poll_s)
    raise TimeoutError(f"job {job_id} not finished after {timeout_s:.0f}s")


def run(base: str, kind: str, payload: dict[str, Any], on_event: EventHandler | None = None,
        **kw: Any) -> dict[str, Any]:
    job = submit(base, kind, payload, **kw)
    return wait(base, job["id"], on_event)
