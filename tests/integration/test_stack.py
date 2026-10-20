"""Integration tests against the live stack (Valkey + oMLX + API + worker on this Mac).

Run with the services up:  pytest -m integration
Skipped automatically when the API is not reachable.
"""

from __future__ import annotations

import os
import socket

import httpx
import pytest

pytestmark = pytest.mark.integration

API = os.environ.get("LAW_SERVER", "http://127.0.0.1:8100")
LLM = "http://127.0.0.1:8000/v1"


def _up(url: str) -> bool:
    try:
        httpx.get(url, timeout=3)
        return True
    except (httpx.HTTPError, OSError):
        return False


needs_stack = pytest.mark.skipif(not _up(f"{API}/healthz"), reason="LAW stack not running")


@needs_stack
def test_health() -> None:
    h = httpx.get(f"{API}/healthz", timeout=5).json()
    assert h["ok"] and h["valkey"]


@needs_stack
def test_llm_endpoint_lists_all_models() -> None:
    ids = {m["id"] for m in httpx.get(f"{LLM}/models", timeout=10).json()["data"]}
    assert {"qwen3.8-27b-8bit", "gemma-4-31b-8bit", "qwen3-coder-next-4bit"} <= ids


@needs_stack
def test_ports_bound_on_lan_interface() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    assert httpx.get(f"http://{ip}:8000/v1/models", timeout=10).status_code == 200
    assert httpx.get(f"http://{ip}:8100/healthz", timeout=10).status_code == 200


@needs_stack
def test_quality_pipeline_end_to_end() -> None:
    r = httpx.post(f"{API}/jobs", json={"kind": "ask", "source": "test",
                   "payload": {"prompt": "State the capital of Japan in one word.", "role": "assistant"}},
                   timeout=30)
    assert r.status_code == 201
    job_id = r.json()["id"]
    with httpx.stream("GET", f"{API}/jobs/{job_id}/stream", timeout=300) as s:
        for _ in s.iter_lines():
            pass
    final = httpx.get(f"{API}/jobs/{job_id}", timeout=10).json()
    assert final["status"] == "done"
    assert "tokyo" in final["result"]["answer"].lower()
    children = httpx.get(f"{API}/jobs/{job_id}/children", timeout=10).json()
    assert {c["kind"] for c in children} == {"draft", "judge", "revise"}
    judge = next(c for c in children if c["kind"] == "judge")
    assert "gemma" in judge["resource"]


def _run_job(kind: str, payload: dict[str, object], timeout: float = 900) -> dict[str, object]:
    r = httpx.post(f"{API}/jobs", json={"kind": kind, "payload": payload, "source": "test"}, timeout=30)
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    with httpx.stream("GET", f"{API}/jobs/{job_id}/stream", timeout=timeout) as s:
        for _ in s.iter_lines():
            pass
    final = httpx.get(f"{API}/jobs/{job_id}", timeout=10).json()
    assert final["status"] == "done", final.get("error")
    return final["result"]


def _wer(ref: str, hyp: str) -> float:
    import re
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower().replace("%", " percent")).split()  # noqa: E731
    r, h = norm(ref), norm(hyp)
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    return d[len(r)][len(h)] / max(1, len(r))


@needs_stack
def test_tts_then_stt_round_trip_word_error_rate() -> None:
    text = "Please schedule the board meeting for Thursday afternoon and send the agenda to every director."
    audio = _run_job("tts", {"text": text})["audio"]
    heard = _run_job("stt", {"audio": audio})["text"]
    assert _wer(text, str(heard)) <= 0.05, heard
