"""The job client keeps waiting across API restarts instead of failing."""

from __future__ import annotations

import httpx
import pytest

from law import client


def test_wait_survives_api_restart(monkeypatch: pytest.MonkeyPatch):
    statuses = iter(["running", "running", "done"])
    streams = {"n": 0}

    def broken_stream(*a, **kw):  # type: ignore[no-untyped-def]
        streams["n"] += 1
        raise httpx.ConnectError("API restarting")

    monkeypatch.setattr(client.httpx, "stream", broken_stream)
    monkeypatch.setattr(client, "get", lambda base, job_id: {"id": job_id, "status": next(statuses)})
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    job = client.wait("http://api", "abc")
    assert job["status"] == "done" and streams["n"] == 3


def test_wait_times_out(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(client.httpx, "stream", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    monkeypatch.setattr(client, "get", lambda base, job_id: None)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        client.wait("http://api", "abc", timeout_s=0.01)


def test_submit_retries_while_api_starts(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def post(url, json, timeout):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(201, json={"id": "j1"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(client.httpx, "post", post)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    assert client.submit("http://api", "ask", {"prompt": "x"})["id"] == "j1"
    assert calls["n"] == 3
