"""Phase 2 tests: interruption limit, DB migration, heartbeat/progress, watchdog,
memory guard, Open WebUI progress lines, probe, launcher flags, webui password reset."""
from __future__ import annotations

import io
import json
import sqlite3
import stat
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from law import webui
from law.paths import get_paths
from law.registry import load
from law.runtime.db import Queue
from law.runtime.gateway import Gateway, progress_line
from law.runtime.modelproc import MlxOpenAIServerLauncher, ModelManager
from law.runtime.probe import haystack, load_probes
from law.runtime.watchdog import Watchdog
from law.runtime.worker import Worker

from test_phase1 import FAKE, chat, download, free_port, run_one, stream_text  # noqa: F401
from test_phase1 import home  # noqa: F401  (fixture)


class Launcher:
    marker = "fake_mlx_server"

    def __init__(self):
        self.contexts: list = []

    def __call__(self, m, path, host, port, context=None):
        self.contexts.append(context)
        return [sys.executable, FAKE, "--port", str(port), "--model-path", str(path)]


def make_rt(pressure="normal", free=1000.0, cfg=None):
    p = get_paths()
    p.logs.mkdir(parents=True, exist_ok=True)
    for mid in ("qwen38-27b", "qwen3-coder-next"):
        download(mid)
    reg = load(p)
    q = Queue(p.queue_db)
    launcher = Launcher()
    mm = ModelManager(p, reg, launcher=launcher, port=free_port(), load_timeout=15, ram_wait=0,
                      free_gb=lambda: free, pressure=lambda: pressure)
    w = Worker(p, reg, q, mm, {"max_attempts": 2, "idle_unload_s": 3600, "first_token_timeout_s": 15,
                               "idle_token_timeout_s": 5, "heartbeat_s": 0.2, **(cfg or {})},
               pressure=lambda: pressure)
    return w, launcher


@pytest.fixture
def rt(home):
    w, _ = make_rt()
    yield w
    w.stop_event.set()
    w.mm.stop()


# ── queue: interruptions + migration ────────────────────────────────────
def test_job_interrupted_too_often_is_failed_not_looped(home):
    q = Queue(get_paths().queue_db)
    jid = chat(q)
    for i in range(2):
        q.claim()
        assert q.requeue_running(max_interruptions=3) == [jid]
    q.claim()
    assert q.requeue_running(max_interruptions=3) == []
    j = q.get(jid)
    assert j["status"] == "failed" and "interrupted 3 times" in j["error"]
    assert q.events(jid)[-1]["type"] == "error"


def test_graceful_stop_counts_as_interruption(home):
    q = Queue(get_paths().queue_db)
    jid = chat(q)
    for _ in range(3):
        q.claim()
        q.requeue(jid, reason="worker stopping", undo_attempt=True, max_interruptions=3)
    assert q.status(jid) == "failed" and q.get(jid)["attempts"] == 0


def test_phase1_database_is_migrated(home):
    db = get_paths().queue_db
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER,
        kind TEXT NOT NULL DEFAULT 'chat', model TEXT NOT NULL, persona TEXT, source TEXT, payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, started REAL, finished REAL,
        error TEXT, result TEXT);
        CREATE TABLE worker(id INTEGER PRIMARY KEY CHECK(id = 1), pid INTEGER, beat REAL, job_id INTEGER,
        model TEXT, state TEXT);
        INSERT INTO jobs(model, payload, created) VALUES('qwen38-27b', '{"messages":[]}', 1);""")
    c.commit(); c.close()
    q = Queue(db)
    assert q.get(1)["interruptions"] == 0 and q.get(1)["state"] is None
    q.beat(1, None, None, "idle", 123.0)
    assert q.worker_info()["progress"] == 123.0
    q.log_watchdog("worker", "restart", "test")
    assert q.watchdog_log(1)[0]["reason"] == "test"


# ── heartbeat / progress ────────────────────────────────────────────────
def test_heartbeat_keeps_beating_while_job_is_busy(rt):
    jid = chat(rt.q, text="SLOW")
    th = threading.Thread(target=rt.run_forever, daemon=True)
    th.start()
    for _ in range(100):
        if any(e["type"] == "deltas" for e in rt.q.events(jid)):
            break
        time.sleep(0.1)
    time.sleep(1.0)
    w = rt.q.worker_info()
    assert w["state"] == "running" and w["job_id"] == jid
    assert w["age_s"] < 1.0 and w["progress_age_s"] < 2.0
    rt.q.cancel(jid)
    rt.stop_event.set()
    th.join(10)


# ── watchdog ────────────────────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def make_wd(home, healthy=True, pressure="normal", **cfg):
    q = Queue(get_paths().queue_db)
    restarts: list[str] = []
    clock = Clock()
    state = {"healthy": healthy}
    wd = Watchdog(load(get_paths()), q, restart=restarts.append, health=lambda: state["healthy"], dash_health=lambda: True,
                  pressure=lambda: pressure, now=clock, cfg={"cooldown_s": 120, **cfg})
    return wd, q, restarts, clock, state


def test_watchdog_restarts_frozen_worker_once_per_cooldown(home):
    # A genuinely frozen worker: the watchdog keeps its normal cadence (small gaps between
    # checks) while the worker's heartbeat stops advancing → restart once, then cooldown.
    wd, q, restarts, clock, _ = make_wd(home)
    q.beat(1, None, None, "idle", clock.t)
    assert wd.check() == [] and restarts == []
    seen: list[str] = []
    for _ in range(10):                                         # ~150s pass at the 15s interval
        clock.t += wd.cfg["interval_s"]
        seen += wd.check()
    assert seen == ["restart worker (heartbeat)"]               # once (cooldown blocks repeats)
    assert restarts == ["com.law.worker"]
    assert "no heartbeat" in q.watchdog_log(1)[0]["reason"]


def test_watchdog_holds_off_restarts_after_system_sleep(home):
    # A single huge gap between checks = the Mac slept: every heartbeat looks stale only
    # because wall time jumped, so nothing is restarted this round (the symptom from the logs:
    # "the Telegram bot has not polled for 3340s" after an overnight sleep).
    wd, q, restarts, clock, _ = make_wd(home)
    q.beat(1, None, None, "idle", clock.t)
    assert wd.check() == [] and restarts == []
    clock.t += 3600                                             # slept an hour (one jump)
    assert wd.check() == [] and restarts == []                  # graced despite the stale heartbeat
    assert q.watchdog_log(1)[0]["target"] == "system"           # the wake was recorded, not a restart


def test_watchdog_still_restarts_a_worker_frozen_through_the_wake(home):
    # If a service never checks back in after a wake, it is still restarted once grace passes.
    wd, q, restarts, clock, _ = make_wd(home, wake_grace_s=120)
    q.beat(1, None, None, "idle", clock.t)
    assert wd.check() == []
    clock.t += 3600                                             # sleep → grace, no restart
    assert wd.check() == []
    seen: list[str] = []
    for _ in range(12):                                         # worker stays silent past the grace window
        clock.t += wd.cfg["interval_s"]
        seen += wd.check()
    assert "restart worker (heartbeat)" in seen


def test_watchdog_leaves_stopped_worker_alone_briefly_then_revives_it(home):
    wd, q, restarts, clock, _ = make_wd(home)
    q.beat(1, None, None, "stopped", clock.t)
    clock.t += 60                                    # a restart in progress: leave it alone
    assert wd.check() == [] and restarts == []
    clock.t += 10_000 - 60                           # `law stop` would have stopped the watchdog too,
    wd._last_tick = clock.t - wd.cfg["interval_s"]   # so a long "stopped" = a reload that didn't come back
    assert wd.check() == ["restart worker (stopped)"] and restarts == ["com.law.worker"]


def test_watchdog_restarts_stalled_job_but_not_a_slow_one(home):
    wd, q, restarts, clock, _ = make_wd(home, stall_s=600)
    jid = chat(q)
    q.claim()
    q.beat(1, jid, "qwen38-27b", "running", clock.t)
    clock.t += 30
    q.beat(1, jid, "qwen38-27b", "running", clock.t - 300)       # alive, progress 5 min ago
    assert wd.check() == [] and restarts == []
    q.beat(1, jid, "qwen38-27b", "running", clock.t - 700)       # alive, no progress 11+ min
    assert wd.check() == ["restart worker (stalled job)"]


def test_watchdog_gateway_needs_consecutive_failures(home):
    wd, q, restarts, clock, state = make_wd(home, healthy=False, gateway_failures=3)
    wd.check(); wd.check()
    state["healthy"] = True
    wd.check()                                                   # recovered → counter reset
    state["healthy"] = False
    wd.check(); wd.check()
    assert restarts == []
    assert wd.check() == ["restart gateway"] and restarts == ["com.law.gateway"]


def test_watchdog_warns_on_critical_pressure_during_job(home):
    wd, q, restarts, clock, _ = make_wd(home, pressure="critical")
    jid = chat(q)
    q.claim()
    q.beat(1, jid, "qwen38-27b", "running", clock.t)
    assert "pressure warning" in wd.check()
    assert wd.check() == []                                      # not spammed
    assert any(e["type"] == "warning" for e in q.events(jid))


def test_watchdog_stall_default_covers_worker_timeouts(home):
    wd = Watchdog(load(get_paths()), Queue(get_paths().queue_db), restart=lambda l: None, health=lambda: True, dash_health=lambda: True)
    assert wd.cfg["stall_s"] >= 1800 + 300


# ── memory guard ────────────────────────────────────────────────────────
def test_memory_guard_refuses_on_critical_pressure(home):
    w, _ = make_rt(pressure="critical", free=3)
    chat(w.q)
    job = run_one(w)
    assert job["status"] == "failed" and job["attempts"] == 1 and "CRITICAL" in job["error"]
    assert w.mm.proc is None
    w.mm.stop()


def test_memory_guard_loads_with_warning_when_not_critical(home):
    w, _ = make_rt(pressure="warn", free=3)
    jid = chat(w.q)
    assert run_one(w)["status"] == "done"
    types = [e["type"] for e in w.q.events(jid)]
    assert "warning" in types
    assert any(e["type"] == "model" and e["data"]["state"] == "waiting_ram" for e in w.q.events(jid))
    w.mm.stop()


def test_critical_pressure_mid_stream_warns_once(home):
    w, _ = make_rt(pressure="critical", free=1000, cfg={"pressure_check_s": 0})
    jid = chat(w.q, text="THINK")
    assert run_one(w)["status"] == "done"
    assert sum(e["type"] == "warning" for e in w.q.events(jid)) == 1
    w.mm.stop()


# ── gateway progress in the chat ────────────────────────────────────────
def test_progress_lines():
    assert "Loading qwen38-27b (~16 GB)" in progress_line("model", {"state": "loading", "model": "qwen38-27b", "gb": 16})
    assert "position 2" in progress_line("status", {"state": "queued"}, 2)
    assert progress_line("status", {"state": "running", "attempt": 1}) is None
    assert "Retrying" in progress_line("status", {"state": "running", "attempt": 2})


def _gateway(rt, progress=True):
    th = threading.Thread(target=rt.run_forever, daemon=True)
    th.start()
    gw = Gateway(rt.paths, rt.reg, rt.q)
    gw.progress = progress
    port = free_port()
    srv = gw.serve("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv, th


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=30)


def test_stream_shows_loading_in_thinking_not_in_answer(rt):
    base, srv, th = _gateway(rt)
    try:
        resp = _post(base + "/v1/chat/completions",
                     {"model": "general", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        text, reasoning, done = stream_text(resp)
        assert done and text == "Hello from qwen38-27b"            # answer untouched
        assert "[law] Loading qwen38-27b" in reasoning and "ready in" in reasoning
        r = json.loads(_post(base + "/v1/chat/completions",
                             {"model": "general", "messages": [{"role": "user", "content": "hi"}]}).read())
        assert "reasoning_content" not in r["choices"][0]["message"]
    finally:
        srv.shutdown(); rt.stop_event.set(); th.join(10)


def test_stream_progress_can_be_turned_off(rt):
    base, srv, th = _gateway(rt, progress=False)
    try:
        resp = _post(base + "/v1/chat/completions",
                     {"model": "general", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        text, reasoning, done = stream_text(resp)
        assert done and text == "Hello from qwen38-27b" and reasoning == ""
    finally:
        srv.shutdown(); rt.stop_event.set(); th.join(10)


def test_progress_stops_once_the_model_answers(rt):
    base, srv, th = _gateway(rt)
    try:
        resp = _post(base + "/v1/chat/completions",
                     {"model": "general", "stream": True, "messages": [{"role": "user", "content": "THINK x"}]})
        order = []
        for raw in resp:
            line = raw.decode().strip()
            if line == "data: [DONE]":
                break
            if line.startswith("data: "):
                for ch in json.loads(line[6:]).get("choices", []):
                    r = ch["delta"].get("reasoning_content")
                    if r:
                        order.append("law" if r.startswith("[law]") else "model")
        assert order[-1] == "model" and "law" not in order[order.index("model"):]
    finally:
        srv.shutdown(); rt.stop_event.set(); th.join(10)


# ── probe ───────────────────────────────────────────────────────────────
def test_haystack_contains_code_and_scales():
    small, big = haystack(1000, "LAW-1"), haystack(8000, "LAW-1")
    assert "LAW-1" in small and len(big) > 6 * len(small)


def probe(home, monkeypatch, sizes, env=None, **payload):
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, str(v))
    w, launcher = make_rt()
    jid = w.q.enqueue("qwen38-27b", {"messages": [{"role": "user", "content": "probe"}], "params": {},
                                     "sizes": sizes, **payload}, kind="probe")
    job = run_one(w)
    return w, launcher, jid, job


def test_probe_measures_and_reports_untested_range(home, monkeypatch):
    w, launcher, jid, job = probe(home, monkeypatch, [512, 1024, 2048])
    assert job["status"] == "done"
    res = json.loads(job["result"])["probe"]
    assert res["proven_context"] == 2048 and all(r["recall"] for r in res["sizes"])
    assert res["suggested_context"] is None                       # registry 32768 > tested → no change
    assert "above what was tested" in res["recommendation"]
    assert res["load_s"] >= 0 and res["speed"]["tokens"] > 0
    assert launcher.contexts[-1] == load().models["qwen38-27b"].context                         # started big enough for every size
    assert load_probes(get_paths())["qwen38-27b"]["proven_context"] == 2048
    assert w.mm.current is None                                   # unloaded afterwards
    assert any(e["type"] == "probe" for e in w.q.events(jid))
    assert not any(e["type"] == "deltas" for e in w.q.events(jid))  # probe text not streamed
    assert all(r["seconds"] >= 0 for r in res["sizes"])


def test_probe_stops_at_first_failure_and_recommends_lowering(home, monkeypatch):
    w, _, _, job = probe(home, monkeypatch, [256, 1024, 4096, 8192], env={"FAKE_RECALL_MAX_CHARS": 6000})
    res = json.loads(job["result"])["probe"]
    assert [r["size"] for r in res["sizes"]] == [256, 1024, 4096]
    assert res["proven_context"] == 1024 and res["failed_at"] == 4096 and res["failed_reason"] == "recall"
    assert res["suggested_context"] == 1024                       # registry 32768 ≥ failed size → lower


def test_next_job_after_probe_uses_normal_context(home, monkeypatch):
    w, launcher, _, _ = probe(home, monkeypatch, [256])
    chat(w.q)
    assert run_one(w)["status"] == "done"
    assert launcher.contexts[-1] is None                          # registry context again
    w.mm.stop()


# ── launcher flags ──────────────────────────────────────────────────────
def test_launcher_only_passes_supported_flags(tmp_path):
    exe = tmp_path / "mlx-openai-server"
    exe.write_text("#!/bin/sh\necho '--model-path --model-type --host --port --context-length'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    reg = load()
    cmd = MlxOpenAIServerLauncher(exe)(reg.models["qwen38-27b"], Path("/m"), "127.0.0.1", 8000, context=131072)
    assert cmd[cmd.index("--context-length") + 1] == "131072"
    assert "--tool-call-parser" not in cmd and "--max-concurrency" not in cmd
    cmd2 = MlxOpenAIServerLauncher(exe)(reg.models["qwen38-27b"], Path("/m"), "127.0.0.1", 8000)
    assert cmd2[cmd2.index("--context-length") + 1] == str(reg.models["qwen38-27b"].context)


# ── Open WebUI password reset ───────────────────────────────────────────
class FakeDocker:
    def __init__(self, users):
        self.users = users
        self.calls = []

    def __call__(self, cmd, stdin):
        self.calls.append((cmd, stdin))
        if "-i" in cmd:
            d = json.loads(stdin)
            return 0, ("updated" if d["e"] in [u[0] for u in self.users] else "notfound") + "\n", ""
        return 0, json.dumps(self.users) + "\n", ""


def test_webui_reset_single_account_never_puts_password_on_command_line():
    fd = FakeDocker([["me@x.com", "admin", "Me"]])
    out = io.StringIO()
    rc = webui.interactive(None, runner=fd, ask=lambda _: "", secret=lambda _: "s3cret!", out=out)
    assert rc == 0 and "Password updated for me@x.com" in out.getvalue()
    cmd, stdin = fd.calls[-1]
    assert "s3cret!" not in " ".join(cmd) and json.loads(stdin) == {"e": "me@x.com", "p": "s3cret!"}


def test_webui_reset_choose_by_number_and_mismatch():
    fd = FakeDocker([["a@x.com", "admin", ""], ["b@x.com", "user", ""]])
    pw = iter(["one", "one"])
    webui.interactive(None, runner=fd, ask=lambda _: "2", secret=lambda _: next(pw), out=io.StringIO())
    assert json.loads(fd.calls[-1][1])["e"] == "b@x.com"
    pw2 = iter(["one", "two"])
    with pytest.raises(webui.WebUIError, match="don't match"):
        webui.interactive("a@x.com", runner=fd, secret=lambda _: next(pw2), out=io.StringIO())


def test_webui_no_accounts_and_container_down():
    out = io.StringIO()
    assert webui.interactive(None, runner=FakeDocker([]), out=out) == 0 and "sign up" in out.getvalue()
    with pytest.raises(webui.WebUIError, match="container"):
        webui.list_users(lambda c, s: (1, "", "No such container: open-webui"))
