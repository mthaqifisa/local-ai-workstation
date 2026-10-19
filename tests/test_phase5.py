"""Phase 5 tests: fixes from the 2026-10-18 review (todo.txt) — prompt-cache leak, dashboard and
gateway request guards, cancel stops the model, retry budget, queue retention, log rotation."""
from __future__ import annotations

from pathlib import Path

from law.paths import get_paths
from law.runtime import modelproc
from law.runtime.modelproc import MlxOpenAIServerLauncher, sweep_prompt_caches

from test_phase1 import chat, home, rt, run_one  # noqa: F401 — fixtures
from test_phase4b import ME, bot, tg  # noqa: F401 — fixtures


# ── A1: prompt-cache leak ───────────────────────────────────────────────
def test_model_temp_folder_is_wiped_on_stop(rt):
    w = rt
    jid = chat(w.q)
    assert run_one(w)["status"] == "done"
    tmp = w.mm.tmp_dir
    caches = list(tmp.glob("mlx-openai-prompt-cache-*"))
    assert caches, "the model server should write its prompt cache inside law's own temp folder"
    w.mm.stop()
    assert not tmp.exists()
    assert jid


def test_sweep_removes_leftovers_only_when_no_server(tmp_path, monkeypatch):
    d = tmp_path / "mlx-openai-prompt-cache-abc"
    d.mkdir()
    (d / "kv").write_bytes(b"x" * 2048)
    other = tmp_path / "keep-me"
    other.mkdir()
    monkeypatch.setattr(modelproc.psutil, "process_iter", lambda *_a, **_k: [])   # no server running
    n, freed = sweep_prompt_caches(tmp_path)
    assert n == 1 and freed >= 2048
    assert not d.exists() and other.exists()

    d.mkdir()

    class P:
        info = {"cmdline": ["/x/bin/mlx-openai-server", "launch"]}
    monkeypatch.setattr(modelproc.psutil, "process_iter", lambda *_a, **_k: [P()])
    assert sweep_prompt_caches(tmp_path) == (0, 0)
    assert d.exists()


def test_launcher_caps_prompt_cache(tmp_path):
    from law.registry import load
    exe = tmp_path / "mlx-openai-server"
    exe.write_text("#!/bin/sh\necho '--model-path --max-bytes INTEGER'\n")
    exe.chmod(0o755)
    m = load().models["qwen38-27b"]
    cmd = MlxOpenAIServerLauncher(exe, prompt_cache_max_gb=2)(m, Path("/m"), "127.0.0.1", 8000)
    assert cmd[cmd.index("--max-bytes") + 1] == str(2 * 1024**3)
    cmd = MlxOpenAIServerLauncher(exe)(m, Path("/m"), "127.0.0.1", 8000)
    assert "--max-bytes" not in cmd


def test_model_temp_folder_lives_under_law_run(rt):
    assert rt.mm.tmp_dir.parent == get_paths().run


# ── A2/A3: request guard (trusted network instead of a login) ───────────
def test_guard_rules():
    from law.runtime.netguard import Guard
    g = Guard(trusted_nets="", allowed_hosts="mymac.example.com")
    assert g.client_ok("127.0.0.1") and g.client_ok("192.168.100.7") and g.client_ok("100.101.1.2")
    assert g.client_ok("::ffff:192.168.1.5") and not g.client_ok("8.8.8.8")
    for h in ("127.0.0.1:8800", "localhost:8800", "mymac", "mymac.local:8800", "[::1]:4000",
              "host.docker.internal:4000", "mymac.example.com", "192.168.100.39:8800", None):
        assert g.host_ok(h), h
    assert not g.host_ok("evil.example.com:8800")             # DNS rebinding
    assert g.origin_ok(None, "127.0.0.1:8800")
    assert g.origin_ok("http://192.168.100.39:8800", "192.168.100.39:8800")
    assert g.origin_ok("http://localhost:8800", "127.0.0.1:8800")
    assert not g.origin_ok("https://evil.example.com", "127.0.0.1:8800")
    assert not g.origin_ok("null", "127.0.0.1:8800")
    hdr = {"Host": "127.0.0.1:8800", "Content-Type": "text/plain"}
    assert "application/json" in g.check("127.0.0.1", hdr, write=True, json_only=True)
    assert g.check("127.0.0.1", hdr, write=False, json_only=True) is None
    g2 = Guard(trusted_nets="10.0.0.0/8", allowed_hosts="")
    assert g2.client_ok("127.0.0.1") and g2.client_ok("10.1.2.3") and not g2.client_ok("192.168.1.2")


def _post(url, body: bytes, headers: dict):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_dashboard_refuses_csrf_and_binds_bind_host(home):
    import json
    import threading
    from law.runtime import dashboard
    httpd = dashboard.build()
    assert httpd.server_address[0] == "127.0.0.1"             # not 0.0.0.0 any more
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/task"
        body = json.dumps({"task": "echo hi"}).encode()
        code, _ = _post(url, body, {"Content-Type": "text/plain"})          # form-style CSRF
        assert code == 403
        code, _ = _post(url, body, {"Content-Type": "application/json", "Origin": "https://evil.example"})
        assert code == 403
        code, _ = _post(url, body, {"Content-Type": "application/json", "Host": "evil.example.com"})
        assert code == 403
        code, raw = _post(url, body, {"Content-Type": "application/json"})
        assert code == 200 and json.loads(raw)["ok"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_gateway_cors_echoes_only_trusted_origin(rt):
    import json
    import urllib.request
    from test_phase1 import free_port
    from law.runtime.gateway import Gateway
    import threading
    port = free_port()
    srv = Gateway(rt.paths, rt.reg, rt.q).serve("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                                         headers={"Origin": f"http://127.0.0.1:{port}"}))
        assert r.headers["Access-Control-Allow-Origin"] == f"http://127.0.0.1:{port}"
        code, _ = _post(f"http://127.0.0.1:{port}/v1/chat/completions",
                        json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode(),
                        {"Content-Type": "application/json", "Origin": "https://evil.example"})
        assert code == 403
        assert rt.q.depth() == 0
    finally:
        srv.shutdown()
        srv.server_close()


# ── A5: cancel really stops the model ───────────────────────────────────
def test_cancel_mid_generation_stops_the_model(rt):
    import threading
    import time
    jid = chat(rt.q, text="SLOW")
    job = rt.q.claim()
    th = threading.Thread(target=rt.handle, args=(job,))
    th.start()
    for _ in range(100):
        if any(e["type"] == "deltas" for e in rt.q.events(jid)):
            break
        time.sleep(0.1)
    assert rt.mm.current
    rt.q.cancel(jid)
    th.join(10)
    assert rt.q.status(jid) == "cancelled"
    assert rt.mm.current is None and not rt.mm.alive()
    assert any(e["type"] == "model" and (e["data"] or {}).get("reason") == "stopping generation"
               for e in rt.q.events(jid))


# ── A6: a non-streamed request whose client hangs up is cancelled ───────
def test_gateway_nonstream_disconnect_cancels(rt):
    import json
    import socket
    import threading
    import time
    from test_phase1 import free_port
    from law.runtime.gateway import Gateway
    port = free_port()
    srv = Gateway(rt.paths, rt.reg, rt.q).serve("127.0.0.1", port)       # no worker: job stays queued
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"model": "general", "messages": [{"role": "user", "content": "hi"}]}).encode()
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        for _ in range(50):
            if rt.q.jobs(1):
                break
            time.sleep(0.1)
        s.close()
        jid = rt.q.jobs(1)[0]["id"]
        for _ in range(50):
            if rt.q.status(jid) == "cancelled":
                break
            time.sleep(0.1)
        assert rt.q.status(jid) == "cancelled"
    finally:
        srv.shutdown()
        srv.server_close()


# ── A7: progress resets the retry budget ────────────────────────────────
def test_fresh_requeue_and_waiting_reset_attempts(home):
    from law.runtime.db import Queue
    q = Queue(get_paths().queue_db)
    jid = chat(q)
    for _ in range(3):
        assert q.claim()["id"] == jid
        assert q.requeue(jid, reason="auto-continue", fresh=True)
    assert q.claim()["attempts"] == 1
    child = chat(q, parent_id=jid)
    assert q.wait_for_children(jid, [child])
    assert q.get(jid)["attempts"] == 0


# ── A8: queue retention ─────────────────────────────────────────────────
def test_queue_prune(home):
    import time
    from law.runtime.db import Queue
    q = Queue(get_paths().queue_db)
    old, new, active = chat(q), chat(q), chat(q)
    for j in (old, new):
        assert q.claim()["id"] == j
        q.add_event(j, "deltas", [{"content": "x"}])
        q.finish(j, "done", result={"message": {"content": "ok"}})
    q._conn().execute("UPDATE jobs SET finished=? WHERE id=?", (time.time() - 20 * 86400, old))
    res = q.prune(keep_events_days=14, keep_jobs_days=180)
    assert res["events"] >= 2 and res["jobs"] == 0
    assert {e["type"] for e in q.events(old)} == {"done"}
    assert any(e["type"] == "deltas" for e in q.events(new))
    q._conn().execute("UPDATE jobs SET finished=? WHERE id=?", (time.time() - 200 * 86400, old))
    assert q.prune(14, 180)["jobs"] == 1
    assert q.get(old) is None and q.get(new) and q.status(active) == "queued"
    idx = {r[1] for r in q._conn().execute("PRAGMA index_list(events)")}
    assert "events_ts" in idx


# ── A9: log rotation ────────────────────────────────────────────────────
def test_rotate_logs_copy_truncate(tmp_path):
    from law.util import rotate_logs
    big, small = tmp_path / "worker.log", tmp_path / "gateway.log"
    big.write_bytes(b"a" * 3000)
    small.write_bytes(b"b" * 10)
    fh = open(big, "ab")                                      # a writer that keeps the file open
    assert rotate_logs(tmp_path, max_mb=0.001, keep=2) == ["worker.log"]
    fh.write(b"new"); fh.flush()
    assert big.read_bytes() == b"new" and (tmp_path / "worker.log.1").stat().st_size == 3000
    big.write_bytes(b"c" * 3000)
    rotate_logs(tmp_path, max_mb=0.001, keep=2)
    assert (tmp_path / "worker.log.2").read_bytes()[:1] == b"a"
    assert (tmp_path / "worker.log.1").read_bytes()[:1] == b"c"
    assert small.read_bytes() == b"b" * 10
    fh.close()


# ── A14: watchdog restarts a hung dashboard ─────────────────────────────
def test_watchdog_restarts_dashboard(home):
    from law.registry import load
    from law.runtime.db import Queue
    from law.runtime.watchdog import Watchdog
    restarts = []
    wd = Watchdog(load(get_paths()), Queue(get_paths().queue_db), restart=restarts.append,
                  health=lambda: True, dash_health=lambda: False, cfg={"gateway_failures": 2})
    wd.check()
    assert restarts == []
    wd.check()
    assert restarts == ["com.law.dashboard"]


# ── A15: /daily catches up after sleep ──────────────────────────────────
def test_daily_schedule_catches_up_after_sleep(bot):
    import time
    from test_phase4b import ME
    b, _w, tg = bot
    now = time.localtime()
    mins = now.tm_hour * 60 + now.tm_min
    if mins < 8 * 60:                       # needs room for "30 min ago" and "7 h ago" today
        return
    hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
    b.state["schedules"] = [
        {"id": 1, "chat": ME, "kind": "daily", "at": hm(mins - 30), "text": "late one", "last_day": ""},
        {"id": 2, "chat": ME, "kind": "daily", "at": hm(mins - 7 * 60), "text": "too late", "last_day": ""}]
    b._due_schedules()
    sent = " ".join(tg.sent())
    assert "late one" in sent and "asleep" in sent and "too late" not in sent
    assert all(s["last_day"] == time.strftime("%Y-%m-%d") for s in b.state["schedules"])
    n = len(tg.sent())
    b._due_schedules()
    assert len(tg.sent()) == n                                  # once per day


# ── A16: vision upload ext can't escape the folder ──────────────────────
def test_vision_upload_ext_whitelisted(home, monkeypatch):
    import base64
    from law.runtime import dashboard, visiongen
    from test_phase2 import make_rt
    w, _ = make_rt()
    monkeypatch.setattr(visiongen, "available", lambda p: True)
    H = type("H", (), {"q": w.q, "paths": w.paths})()
    res = dashboard.Handler._enqueue_vision.__get__(H)({"image_b64": base64.b64encode(b"img").decode(),
                                                        "ext": "/../../evil"})
    import json
    img = json.loads(w.q.get(res["id"])["payload"])["image"]
    assert img.endswith(".png") and Path(img).parent == w.paths.outputs / "vision"
    w.mm.stop()


# ── A17: only law's own .screenshots folder is removed ──────────────────
def test_screenshots_folder_kept_if_user_owned(rt, tmp_path, monkeypatch):
    import law.agent.runner as runner
    from law.agent.runner import SCREENSHOT_MARKER
    mine = tmp_path / "proj-mine"
    (mine / ".screenshots").mkdir(parents=True)
    (mine / ".screenshots" / "keep.png").write_bytes(b"x")
    ours = tmp_path / "proj-ours"
    (ours / ".screenshots").mkdir(parents=True)
    (ours / ".screenshots" / SCREENSHOT_MARKER).write_text("x")

    def refuse(w, job):
        raise runner.AgentRefused("stop")             # end the job at once → cleanup runs
    monkeypatch.setattr(runner, "run_agent", refuse)
    for proj in (mine, ours):
        rt.q.enqueue("qwen38-27b", {"cwd": str(proj)}, kind="agent")
        rt.handle(rt.q.claim())
    assert (mine / ".screenshots" / "keep.png").exists()
    assert not (ours / ".screenshots").exists()


# ── A18/B: media commands from the CLI ──────────────────────────────────
def test_media_cli_commands_enqueue(home, tmp_path):
    import json
    from law.cli import main
    from law.runtime.db import Queue
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    for argv in (["image", "a fox", "--seed", "3", "-d"], ["speak", "hi", "--voice", "bm_lewis", "-d"],
                 ["video", "waves", "--seconds", "4", "-d"], ["vision", str(img), "what", "-d"],
                 ["transcribe", str(img), "-d"], ["--image", "legacy fox", "-d"]):
        assert main(argv) == 0, argv
    jobs = {j["kind"]: j for j in Queue(get_paths().queue_db).jobs(10)}
    assert set(jobs) == {"image", "tts", "video", "vision", "stt"}
    assert json.loads(jobs["tts"]["payload"])["params"]["voice"] == "bm_lewis"
    assert json.loads(jobs["vision"]["payload"])["image"] == str(img.resolve())


# ── A19: Anthropic stream reports real usage ────────────────────────────
def test_anthropic_stream_usage():
    from law.runtime.anthropic import StreamTranslator
    tr = StreamTranslator("claude-x", "msg_1")
    list(tr.start())
    list(tr.delta({"content": "hi"}))
    ev = dict(tr.finish("stop", {"prompt_tokens": 120, "completion_tokens": 7}))
    assert ev["message_delta"]["usage"] == {"output_tokens": 7, "input_tokens": 120}
    tr2 = StreamTranslator("claude-x", "msg_2")
    list(tr2.delta({"content": "a"}))
    assert dict(tr2.finish("stop"))["message_delta"]["usage"] == {"output_tokens": 1}


# ── B5: unload the model on demand ──────────────────────────────────────
def test_unload_job_frees_model(rt):
    assert chat(rt.q) and run_one(rt)["status"] == "done"
    assert rt.mm.current
    rt.q.enqueue(rt.mm.current, {}, kind="unload", priority=100)
    job = run_one(rt)
    assert job["status"] == "done" and rt.mm.current is None


# ── B6: a measured context can be applied as an override ────────────────
def test_set_context_override(home):
    from law.modelcmds import set_context
    from law.registry import load
    p = get_paths()
    set_context(p, "qwen36-35b", 40960)
    set_context(p, "qwen36-35b", 49152)
    reg = load(p)
    assert reg.models["qwen36-35b"].context == 49152 and reg.models["qwen36-35b"].source == "override"
    assert reg.models["qwen36-35b"].repo.startswith("unsloth/")


# ── B9: backup + restore round trip ─────────────────────────────────────
def test_backup_restore_roundtrip(home):
    import stat
    import tarfile
    from law.backupcmds import create, restore
    from law.runtime.db import Queue
    from law.runtime.store import Store
    from law.util import write_env
    p = get_paths()
    p.config.mkdir(parents=True, exist_ok=True)
    p.user_models.write_text("models: []\n")
    write_env(p.env_file, {"HF_TOKEN": "hf_x"})
    Store(p.agent_db).add_memory(home / "proj", "uses postgres")
    chat(Queue(p.queue_db))
    arc = create()
    assert stat.S_IMODE(arc.stat().st_mode) == 0o600
    names = tarfile.open(arc).getnames()
    assert ".env" in names and "state/agent.db" in names and "config/models.user.yaml" in names
    assert ".env" not in tarfile.open(create(p.backups / "nos.tar.gz", secrets=False)).getnames()
    p.env_file.unlink()
    p.agent_db.unlink()
    assert {"config", "state", ".env"} <= set(restore(arc))
    assert "hf_x" in p.env_file.read_text() and stat.S_IMODE(p.env_file.stat().st_mode) == 0o600
    assert "uses postgres" in Store(p.agent_db).memory_markdown(home / "proj")


# ── C3: notifications only for long, top-level, local jobs ──────────────
def test_should_notify_rules(monkeypatch):
    from law.runtime import notify
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    j = {"kind": "agent", "source": "dashboard", "parent_id": None}
    assert notify.should_notify(j, "done", 120, {})
    assert not notify.should_notify(j, "done", 10, {})
    assert not notify.should_notify({**j, "parent_id": 3}, "done", 120, {})
    assert not notify.should_notify({**j, "source": "telegram"}, "done", 120, {})
    assert not notify.should_notify({**j, "kind": "chat"}, "done", 120, {})
    assert not notify.should_notify(j, "cancelled", 120, {})
    assert not notify.should_notify(j, "done", 120, {"enabled": False})


# ── C18: job as Markdown ────────────────────────────────────────────────
def test_job_markdown(home):
    from law.runtime.db import Queue
    from law.runtime.report import job_markdown
    q = Queue(get_paths().queue_db)
    jid = q.enqueue("qwen36-35b", {"task": "build a todo app", "cwd": "/tmp/p"}, persona="coder", kind="agent")
    q.claim()
    q.add_event(jid, "snapshot", {"id": "abc123", "project": "/tmp/p"})
    q.add_event(jid, "agent", {"step": 1, "think": "plan it"})
    q.add_event(jid, "agent", {"step": 1, "tool": "run_shell", "args": "npm init", "running": True})
    q.add_event(jid, "agent", {"step": 1, "tool": "run_shell", "seconds": 1.2, "output": "ok"})
    q.finish(jid, "done", result={"message": {"content": "Built it."}})
    md = job_markdown(q, jid)
    for part in ("# Job", "build a todo app", "## Steps", "`run_shell`", "## Result", "Built it.",
                 "law rollback /tmp/p --to abc123"):
        assert part in md, part


# ── C11: plan first, then run the approved spec (dashboard) ─────────────
def test_dashboard_plan_then_run(home):
    import json
    from law.runtime import dashboard
    from law.runtime.db import Queue
    from law.registry import load
    p = get_paths()
    q = Queue(p.queue_db)
    H = type("H", (), {"q": q, "paths": p, "reg": load(p)})()
    r = dashboard.Handler._enqueue.__get__(H)({"task": "build x", "persona": "coder", "plan_only": True})
    assert json.loads(q.get(r["id"])["payload"])["plan_only"] is True
    q.claim()
    q.finish(r["id"], "done", result={"message": {"content": "# Task: X"},
                                       "plan": {"task": "build x", "spec": "# Task: X"}})
    det = dashboard.Handler._job_detail.__get__(H)(r["id"])
    assert det["plan"]["spec"] == "# Task: X" and det["project"]
    r2 = dashboard.Handler._enqueue.__get__(H)({"task": "build x", "persona": "coder", "project": det["project"],
                                                "spec_text": det["plan"]["spec"]})
    pl = json.loads(q.get(r2["id"])["payload"])
    assert pl["spec_text"] == "# Task: X" and pl["translate"] is False and "plan_only" not in pl


# ── E7 finding: agents' .screenshots scratch is never part of an undo snapshot ──
def test_undo_ignores_screenshots(tmp_path):
    from law.undo import Undo
    proj = tmp_path / "p"
    (proj / ".screenshots").mkdir(parents=True)
    (proj / ".screenshots" / ".law-temp").write_text("x")
    (proj / "a.txt").write_text("a")
    u = Undo(proj)
    u.snapshot()
    files = u._git("ls-files").split()
    assert files == ["a.txt"]


# ── E finding: Telegram live line with a server-side reasoning split ────
def test_live_line_with_split_reasoning(bot):
    b, _w, _tg = bot
    assert b._live_line({"think": "pondering", "stream": ""}).startswith("🧠 pondering")
    assert b._live_line({"think": "pondering", "stream": "Hello there"}) == "💬 Hello there"
    assert b._live_line({"stream": "still thinking"}).startswith("🧠")             # old style: no split
    assert b._live_line({"stream": "thought</think>Answer"}) == "💬 Answer"


# ── E-F3: a hung long-poll can't freeze the bot ─────────────────────────
def test_get_updates_hard_deadline(monkeypatch):
    import threading
    import time
    from law.telegram import Api
    api = Api("1:x", base="http://127.0.0.1:9")
    api.POLL_GRACE_S = 0.3
    gate = threading.Event()
    monkeypatch.setattr(api, "_long_poll", lambda o, t: gate.wait(30) or [])
    t0 = time.time()
    try:
        api.get_updates(None, 0.2)
        raise AssertionError("should have timed out")
    except OSError as e:
        assert "did not return" in str(e)
    assert time.time() - t0 < 2
    gate.set()
    monkeypatch.setattr(api, "_long_poll", lambda o, t: [{"update_id": 5}])
    assert api.get_updates(None, 0.2) == [{"update_id": 5}]


# ── E-F4: the worker frees ACE-Step's memory when a model needs it ──────
def test_model_load_reclaims_other_gpu_users(home):
    from law.registry import load
    from law.runtime.modelproc import ModelManager
    p = get_paths()
    reg = load(p)
    free = {"gb": 5.0}
    calls, events = [], []
    mm = ModelManager(p, reg, launcher=None, port=1, ram_wait=0, free_gb=lambda: free["gb"],
                      pressure=lambda: "normal")
    mm.emit = lambda t, d: events.append((t, d))

    def reclaim():
        calls.append(1)
        free["gb"] = 100.0
        return "stopped the music studio"
    mm.reclaim = reclaim
    mm._wait_ram(reg.models["qwen38-27b"])
    assert calls == [1] and ("warning", {"message": "stopped the music studio"}) in events
    calls.clear()
    mm._wait_ram(reg.models["qwen38-27b"])                     # enough RAM → nothing stopped
    assert calls == []


# ── Telegram E2E findings: time + weather grounding ─────────────────────
def test_weather_place_and_lookup_words():
    from law.telegram import needs_lookup, weather_place
    assert weather_place("What time is it in Tokyo right now, and what's the weather there today?") == "Tokyo"
    assert weather_place("cuaca di Kuala Lumpur hari ini") == "Kuala Lumpur"
    assert weather_place("weather tomorrow?") is None and weather_place("build me an app in React") is None
    assert needs_lookup("cuaca di Kuala Lumpur hari ini") and needs_lookup("berita terkini AI")


def test_prompts_carry_the_current_time(tmp_path):
    import time
    from law.agent.prompts import now_line, system_prompt
    assert time.strftime("%Y") in now_line() and "UTC" in now_line()
    sp = system_prompt("general", tmp_path, can_delegate=False, personas=["general"])
    assert "Now: " + time.strftime("%A, %d %B %Y") in sp


def test_time_place():
    from law.telegram import needs_lookup, time_place
    assert time_place("What time is it in Tokyo right now?") == "Tokyo"
    assert time_place("jam berapa di London sekarang") == "London"
    assert time_place("what is a mutex") is None
    assert needs_lookup("what time is it in New York")


# ── internet first: search by default, offline only for self-contained work ──
def test_needs_lookup_is_the_default():
    from law.telegram import needs_lookup
    for q in ("who is the CEO of OpenAI", "explain how a hash map works", "best laptop for coding",
              "how do I install mlx-lm", "what is the population of Malaysia", "siapa perdana menteri Malaysia"):
        assert needs_lookup(q), q
    for q in ("what is 17 * 23?", "solve x^2 - 4 = 0", "write a python function to reverse a list",
              "translate 'good morning' to Japanese", "write me a short poem about rain", "thanks!", "hi",
              "rewrite this paragraph to be more formal"):
        assert not needs_lookup(q), q
    assert needs_lookup("write a script that prints today's bitcoin price")   # live words win


def test_short_followup_is_searched_with_its_question(bot, monkeypatch):
    b, _w, _tg = bot
    seen = []
    monkeypatch.setattr(b, "_web_lookup", lambda q, n=5: seen.append(q) or "- x")
    b.answer(ME, "what's the weather in Penang today?")
    b.answer(ME, "and tomorrow?")
    assert seen[-1].startswith("what's the weather in Penang today?") and seen[-1].endswith("and tomorrow?")


# ── Telegram E2E finding: web_fetch decodes compressed pages ────────────
def test_decompress_gzip_and_truncated():
    import gzip
    import zlib
    from law.agent.tools import _decompress
    body = ("<html><body>" + "Python 3.15.0 released " * 400 + "</body></html>").encode()
    gz = gzip.compress(body)
    assert _decompress(gz, "gzip") == body
    assert _decompress(gz, "") == body                                 # undeclared but gzip magic
    assert _decompress(gz[: len(gz) // 2], "gzip").startswith(b"<html><body>")   # cut at max_bytes
    assert _decompress(zlib.compress(body), "deflate") == body
    assert _decompress(b"plain", "") == b"plain"


# ── Telegram E2E finding: a network blip must not drop a message ────────
def test_send_retries_network_errors(bot, monkeypatch):
    b, _w, _tg = bot
    b.NET_RETRIES = (0, 0, 0)
    calls = []
    real = b.api.call

    def flaky(method, **params):
        calls.append(method)
        if len(calls) <= 2:
            raise ConnectionResetError(54, "Connection reset by peer")
        return real(method, **params)
    monkeypatch.setattr(b.api, "call", flaky)
    assert b.send(ME, "the agent's report") is not None
    assert calls == ["sendMessage"] * 3


def test_md_blockquote():
    from law.telegram import md_to_html
    out = md_to_html("x\n> **1 USD = 4.08 MYR**\n> line2\ny 3 > 2")
    assert "<blockquote><b>1 USD = 4.08 MYR</b>\nline2</blockquote>" in out and "y 3 &gt; 2" in out
