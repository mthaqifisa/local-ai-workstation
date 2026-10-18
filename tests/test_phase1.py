"""Phase 1 tests: queue, single worker, model swapping, gateway, services step.
A fake model server (fake_mlx_server.py) stands in for mlx-openai-server."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest
import yaml

from law.engine import Ctx, run_steps
from law.paths import get_paths
from law.registry import load
from law.runtime.db import Queue
from law.runtime.gateway import Gateway
from law.runtime.modelproc import ModelManager, kill_from_pidfile
from law.runtime.worker import Worker
from law.steps import build_steps
from law.util import atomic_write

from fakes import FakeSystem

FAKE = str(Path(__file__).with_name("fake_mlx_server.py"))
FAKE_ENV = ("LAW_HOME", "HF_HOME", "HF_HUB_CACHE", "FAKE_DIE", "FAKE_USAGE", "FAKE_SECRET_SLEEP",
            "FAKE_500_OVER_CHARS", "FAKE_500_HTTP", "FAKE_RECALL_MAX_CHARS")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in FAKE_ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def download(mid: str):
    """Make a model look downloaded; its snapshot folder is named after the id."""
    reg = load(get_paths())
    repo = reg.models[mid].repo
    d = get_paths().hf_cache / ("models--" + repo.replace("/", "--"))
    (d / "snapshots" / mid).mkdir(parents=True, exist_ok=True)
    (d / "refs").mkdir(exist_ok=True)
    (d / "refs" / "main").write_text(mid)


class Launcher:
    marker = "fake_mlx_server"

    def __call__(self, m, path, host, port, **kw):
        return [sys.executable, FAKE, "--port", str(port), "--model-path", str(path)]


@pytest.fixture
def rt(home):
    """A worker wired to the fake model server, with small timeouts."""
    p = get_paths()
    p.logs.mkdir(parents=True, exist_ok=True)
    for mid in ("qwen38-27b", "qwen36-35b", "qwen3-coder-next"):
        download(mid)
    reg = load(p)
    q = Queue(p.queue_db)
    mm = ModelManager(p, reg, launcher=Launcher(), port=free_port(), load_timeout=15, ram_wait=0,
                      free_gb=lambda: 1000)
    w = Worker(p, reg, q, mm, {"max_attempts": 2, "idle_unload_s": 3600, "first_token_timeout_s": 15,
                               "idle_token_timeout_s": 5})
    yield w
    w.stop_event.set()
    mm.stop()


def chat(q, model="qwen38-27b", text="hi", **kw):
    return q.enqueue(model, {"messages": [{"role": "user", "content": text}], "params": {}}, **kw)


def run_one(w):
    job = w.q.claim()
    assert job is not None
    w.handle(job)
    return w.q.get(job["id"])


# ── queue ───────────────────────────────────────────────────────────────
def test_queue_order_cancel_and_finish_race(home):
    q = Queue(get_paths().queue_db)
    a, b = chat(q), chat(q)
    hi = chat(q, priority=5)
    assert q.position(hi) == 1 and q.position(a) == 2
    assert q.claim()["id"] == hi
    assert q.cancel(hi)
    assert q.finish(hi, "done", result={}) is False          # cancelled stays cancelled
    assert q.status(hi) == "cancelled"
    assert q.claim()["id"] == a
    assert q.requeue_running() == [a]
    assert q.status(a) == "queued" and q.depth() == 2


def test_two_workers_never_claim_the_same_job(home):
    q = Queue(get_paths().queue_db)
    ids = [chat(q) for _ in range(40)]
    got: list[int] = []
    lock = threading.Lock()

    def grab():
        q2 = Queue(get_paths().queue_db)
        while (j := q2.claim()) is not None:
            with lock:
                got.append(j["id"])
    ts = [threading.Thread(target=grab) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == ids


# ── worker + model manager ──────────────────────────────────────────────
def test_chat_job_streams_and_finishes(rt):
    jid = chat(rt.q, text="THINK please")
    job = run_one(rt)
    assert job["status"] == "done"
    msg = json.loads(job["result"])["message"]
    assert msg["content"] == "Hello from qwen38-27b"
    assert msg["reasoning_content"] == "hmm "
    types = [e["type"] for e in rt.q.events(jid)]
    assert "model" in types and "deltas" in types and types[-1] == "done"


def test_same_model_is_reused_different_model_swaps_first(rt):
    chat(rt.q)
    run_one(rt)
    pid1 = rt.mm.proc.pid
    chat(rt.q)
    run_one(rt)
    assert rt.mm.proc.pid == pid1                              # reused, not restarted
    chat(rt.q, model="qwen3-coder-next")
    job = run_one(rt)
    assert json.loads(job["result"])["message"]["content"] == "Hello from qwen3-coder-next"
    assert not psutil.pid_exists(pid1) or psutil.Process(pid1).status() == psutil.STATUS_ZOMBIE
    assert rt.mm.current == "qwen3-coder-next"


def test_tool_calls_are_assembled(rt):
    chat(rt.q, text="TOOL")
    res = json.loads(run_one(rt)["result"])
    assert res["finish_reason"] == "tool_calls"
    tc = res["message"]["tool_calls"][0]
    assert tc["function"] == {"name": "get_time", "arguments": '{"tz":"UTC"}'} and tc["id"] == "call1"


def test_oversize_model_refused_without_retry(rt, home):
    atomic_write(get_paths().user_models, yaml.safe_dump({"models": [
        {"id": "giant", "repo": "x/giant", "kind": "lm", "weights_gb": 300}]}))
    rt.mm.reg = rt.reg = load(get_paths())
    chat(rt.q, model="giant")
    job = run_one(rt)
    assert job["status"] == "failed" and job["attempts"] == 1 and "limit" in job["error"]


def test_not_downloaded_model_fails_with_hint(rt):
    chat(rt.q, model="deepseek-r1-70b")
    job = run_one(rt)
    assert job["status"] == "failed" and "law pull deepseek-r1-70b" in job["error"]


def test_load_failure_retries_then_fails_fast_with_log(rt, monkeypatch):
    monkeypatch.setenv("FAKE_DIE", "1")
    jid = chat(rt.q)
    t0 = time.time()
    run_one(rt)
    assert rt.q.status(jid) == "queued"                         # retry 1
    job = run_one(rt)
    assert job["status"] == "failed" and "out of memory" in job["error"]
    assert time.time() - t0 < 15


def test_model_crash_mid_stream_is_retried(rt):
    jid = chat(rt.q, text="CRASH")
    run_one(rt)
    assert rt.q.status(jid) == "queued"
    assert not rt.mm.alive()


def test_cancel_mid_stream_stops_quickly(rt):
    jid = chat(rt.q, text="SLOW")
    job = rt.q.claim()
    th = threading.Thread(target=rt.handle, args=(job,))
    th.start()
    for _ in range(100):
        if any(e["type"] == "deltas" for e in rt.q.events(jid)):
            break
        time.sleep(0.1)
    t0 = time.time()
    rt.q.cancel(jid)
    th.join(10)
    assert not th.is_alive() and time.time() - t0 < 3
    assert rt.q.status(jid) == "cancelled"


def test_worker_stop_puts_job_back(rt):
    jid = chat(rt.q, text="SLOW")
    job = rt.q.claim()
    th = threading.Thread(target=rt.handle, args=(job,))
    th.start()
    time.sleep(1.5)
    rt.stop_event.set()
    th.join(10)
    j = rt.q.get(jid)
    assert j["status"] == "queued" and j["attempts"] == 0


def test_crash_recovery_requeues_and_kills_orphan(rt):
    p = get_paths()
    port = free_port()
    orphan = subprocess.Popen([sys.executable, FAKE, "--port", str(port), "--model-path", "/x/orphan"],
                              start_new_session=True)
    time.sleep(1)                                              # let it exec before we inspect it
    p.run.mkdir(parents=True, exist_ok=True)
    (p.run / "model.pid").write_text(json.dumps({"pid": orphan.pid, "marker": "fake_mlx_server"}))
    jid = chat(rt.q)
    rt.q.claim()                                               # simulate: was running when we crashed
    rt.startup()
    orphan.wait(10)
    assert orphan.returncode is not None
    assert rt.q.status(jid) == "queued"


def test_pidfile_never_kills_unrelated_process(home):
    p = get_paths()
    p.run.mkdir(parents=True, exist_ok=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (p.run / "model.pid").write_text(json.dumps({"pid": other.pid, "marker": "fake_mlx_server"}))
    assert kill_from_pidfile(p.run / "model.pid") is False
    assert other.poll() is None
    other.kill()


def test_idle_unload(rt):
    rt.cfg["idle_unload_s"] = 0.5
    chat(rt.q)
    th = threading.Thread(target=rt.run_forever)
    th.start()
    for _ in range(100):
        if rt.q.status(1) == "done":
            break
        time.sleep(0.1)
    assert rt.mm.current == "qwen38-27b"
    time.sleep(2)
    assert rt.mm.current is None
    rt.stop_event.set()
    th.join(10)
    assert rt.q.worker_info()["state"] == "stopped"


# ── gateway ─────────────────────────────────────────────────────────────
@pytest.fixture
def gw(rt):
    th = threading.Thread(target=rt.run_forever, daemon=True)
    th.start()
    port = free_port()
    srv = Gateway(rt.paths, rt.reg, rt.q).serve("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", rt
    srv.shutdown()
    rt.stop_event.set()
    th.join(10)


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    return urllib.request.urlopen(req, timeout=30)


def test_gateway_models_and_errors(gw):
    base, _ = gw
    ids = [m["id"] for m in json.loads(urllib.request.urlopen(base + "/v1/models").read())["data"]]
    assert "general" in ids and "coder" in ids and "qwen38-27b" in ids
    assert "deep-reasoner" not in ids                          # its model isn't downloaded
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/v1/chat/completions", {"model": "nope", "messages": [{"role": "user", "content": "x"}]})
    assert e.value.code == 404
    h = json.loads(urllib.request.urlopen(base + "/health").read())
    assert h["ok"] is True


def test_gateway_non_stream_via_persona(gw):
    base, _ = gw
    r = json.loads(post(base + "/v1/chat/completions",
                        {"model": "coder", "messages": [{"role": "user", "content": "hi"}]}).read())
    assert r["choices"][0]["message"]["content"] == "Hello from qwen3-coder-next"
    assert r["model"] == "coder"


def stream_text(resp):
    text, reasoning, done = "", "", False
    for raw in resp:
        line = raw.decode().strip()
        if line == "data: [DONE]":
            done = True
            break
        if line.startswith("data: "):
            for ch in json.loads(line[6:]).get("choices", []):
                text += ch["delta"].get("content") or ""
                reasoning += ch["delta"].get("reasoning_content") or ""
    return text, reasoning, done


def test_gateway_stream(gw):
    base, _ = gw
    resp = post(base + "/v1/chat/completions",
                {"model": "general", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    text, _, done = stream_text(resp)
    assert done and text == "Hello from qwen38-27b"


def test_gateway_requests_take_turns(gw):
    base, rt = gw
    results = {}

    def ask(i, model):
        r = json.loads(post(base + "/v1/chat/completions",
                            {"model": model, "messages": [{"role": "user", "content": "hi"}]}).read())
        results[i] = r["choices"][0]["message"]["content"]
    ts = [threading.Thread(target=ask, args=(i, m)) for i, m in
          enumerate(["general", "coder", "general", "orchestrator"])]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert len(results) == 4
    jobs = rt.q.jobs(10)
    spans = sorted((j["started"], j["finished"]) for j in jobs)
    for (s1, f1), (s2, _) in zip(spans, spans[1:]):
        assert s2 >= f1 - 0.05                                 # never overlapping


def test_gateway_client_disconnect_cancels_job(gw):
    base, rt = gw
    body = json.dumps({"model": "general", "stream": True,
                       "messages": [{"role": "user", "content": "SLOW"}]}).encode()
    host, port = base.split("//")[1].split(":")
    s = socket.create_connection((host, int(port)))
    s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
              + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    buf = b""
    while b"Hello" not in buf:
        buf += s.recv(4096)
    s.close()
    jid = rt.q.jobs(1)[0]["id"]
    for _ in range(60):
        if rt.q.status(jid) == "cancelled":
            break
        time.sleep(0.2)
    assert rt.q.status(jid) == "cancelled"


# ── services install step ───────────────────────────────────────────────
def test_services_step_installs_reloads_and_prunes(home):
    fs = FakeSystem(get_paths().hf_cache)
    run_steps(Ctx(paths=get_paths(), sys=fs), build_steps(), on_macos=True)
    la = home / "Library" / "LaunchAgents"
    from law.steps.services import SERVICES, label
    want = {label(n) for n in SERVICES}
    assert all((la / f"{lab}.plist").exists() for lab in want)
    assert set(fs.loaded) == want
    fs.calls.clear()
    rep = run_steps(Ctx(paths=get_paths(), sys=fs), build_steps(), on_macos=True)
    assert rep.added + rep.updated + rep.removed == 0 and fs.calls == []
    # registry change → services reload; stale com.law.* removed; colima kept
    atomic_write(get_paths().user_models, yaml.safe_dump({"models": [
        {"id": "mine", "repo": "me/x", "kind": "lm", "weights_gb": 3}]}))
    (la / "com.law.old.plist").write_text("<plist/>")
    fs.loaded["com.law.old"] = "x"
    fs.loaded.pop("com.law.worker")                            # crashed / unloaded
    fs.calls.clear()
    run_steps(Ctx(paths=get_paths(), sys=fs), build_steps(), on_macos=True)
    assert "load com.law.worker" in fs.calls and "load com.law.gateway" in fs.calls
    assert "unload com.law.old" in fs.calls and not (la / "com.law.old.plist").exists()
    assert (la / "com.law.colima.plist").exists()
