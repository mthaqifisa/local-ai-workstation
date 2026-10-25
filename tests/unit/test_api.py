"""The job API over a fakeredis store (no worker, no real services)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import fakeredis
import httpx
import pytest

from law.api import create_app
from law.paths import LawPaths
from law.queue.jobs import Job, Status
from law.queue.store import JobStore


def resolve(job: Job) -> Job:
    """Trivial stand-in for the registry resolver; rejects unknown kinds like the real one."""
    table = {"ask": ("llm:a", "heavy", 30.0), "search": ("", "none", 0.0)}
    if job.kind not in table:
        raise ValueError(f"unknown job kind {job.kind!r}")
    job.resource, job.lane, job.mem_gb = table[job.kind]  # type: ignore[assignment]
    return job


@pytest.fixture()
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, JobStore]]:
    store = JobStore(fakeredis.FakeAsyncRedis(decode_responses=True))
    app = create_app(LawPaths.from_env(), store=store, resolve=resolve)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://law") as http,
        app.router.lifespan_context(app),
    ):
        yield http, store
    await store.close()


async def test_submit_returns_queued_job_with_resolved_resource(client):
    http, store = client
    r = await http.post("/jobs", json={"kind": "ask", "payload": {"q": "hi"}})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["resource"] == "llm:a" and body["lane"] == "heavy"
    # The job is really in the store's queue, ready for the worker.
    assert [j.id for j in await store.queued()] == [body["id"]]


async def test_unknown_kind_is_422_not_500(client):
    http, _ = client
    r = await http.post("/jobs", json={"kind": "nope"})
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]


async def test_get_job_and_history(client):
    http, _ = client
    jid = (await http.post("/jobs", json={"kind": "ask"})).json()["id"]
    assert (await http.get(f"/jobs/{jid}")).json()["id"] == jid
    assert (await http.get("/jobs/missing")).status_code == 404
    history = (await http.get("/jobs")).json()
    assert [j["id"] for j in history] == [jid]


async def test_cancel_marks_queued_job_cancelled(client):
    http, store = client
    jid = (await http.post("/jobs", json={"kind": "ask"})).json()["id"]
    r = await http.post(f"/jobs/{jid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == Status.CANCELLED.value
    assert (await store.get(jid)).status == Status.CANCELLED


async def test_events_endpoint_replays_history(client):
    http, store = client
    jid = (await http.post("/jobs", json={"kind": "ask"})).json()["id"]
    await store.event(jid, "progress", {"message": "working"})
    events = (await http.get(f"/jobs/{jid}/events")).json()
    assert [e["event"] for e in events] == ["queued", "progress"]


async def test_stream_emits_events_until_terminal(client):
    http, store = client
    jid = (await http.post("/jobs", json={"kind": "ask"})).json()["id"]
    await store.finish(await store.require(jid), Status.DONE, result={"answer": "x"})
    seen: list[str] = []
    async with http.stream("GET", f"/jobs/{jid}/stream") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                seen.append(json.loads(line[len("data:"):].strip())["event"])
    assert "queued" in seen and "done" in seen


async def test_files_rejects_path_traversal(client):
    http, _ = client
    assert (await http.get("/files", params={"path": "../../etc/passwd"})).status_code == 400
    assert (await http.get("/files", params={"path": "images/none.png"})).status_code == 404


async def test_healthz_reports_queue_depth(client):
    http, _ = client
    await http.post("/jobs", json={"kind": "ask"})
    body = (await http.get("/healthz")).json()
    assert body["ok"] is True and body["valkey"] is True and body["queued"] == 1



async def test_send_messages_the_owner_and_reports_delivery_errors(client, monkeypatch):
    from law import deliver

    c, _ = client
    sent: list[str] = []

    def fake(env_file, files, message, transport=None):  # type: ignore[no-untyped-def]
        sent.append(message)
        return 1

    monkeypatch.setattr(deliver, "to_telegram", fake)
    r = await c.post("/send", json={"text": "2 items expire today"})
    assert r.status_code == 200 and r.json() == {"chats": 1} and sent == ["2 items expire today"]
    assert (await c.post("/send", json={"text": ""})).status_code == 422

    def broken(*a, **k):  # type: ignore[no-untyped-def]
        raise deliver.DeliveryError("Telegram is not set up")

    monkeypatch.setattr(deliver, "to_telegram", broken)
    r = await c.post("/send", json={"text": "x"})
    assert r.status_code == 503 and "not set up" in r.json()["detail"]
