"""Phase 4b tests: Telegram bot (private access, agent / ask / plan→go / raw, progress
message, final answer, cancel, resume, undo, persona, project, polling + offset),
services + watchdog integration, `law configure`, OpenCode provider, and chat requests
with tools answered as one whole reply (OpenCode / Claude Code path)."""
from __future__ import annotations

import argparse
import io
import json
import shutil
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from law import opencode, setupcmds
from law.engine import Ctx
from law.paths import get_paths
from law.registry import load
from law.runtime.db import Queue
from law.runtime.gateway import Gateway
from law.runtime.store import Store
from law.runtime.watchdog import Watchdog
from law.steps import opencode as oc_step
from law.steps.services import label, wanted
from law.telegram import Api, Bot, beat_age, beat_file, creds, event_line, live_split, split_text, tail
from law.util import write_env

from fakes import FakeSystem
from test_phase1 import free_port
from test_phase1 import home  # noqa: F401  (fixture)
from test_phase2 import make_rt

GIT = shutil.which("git") is not None
GOOD = "123:GOOD"
ME = 4242


# ── fake Telegram Bot API ───────────────────────────────────────────────
class FakeTG:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.next_update = 1
        self.next_msg = 100
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if self.headers.get("Content-Type", "").startswith("multipart/"):
                    body = {"_multipart": True, "_bytes": len(raw)}   # don't JSON-parse a file upload
                else:
                    body = json.loads(raw or b"{}")
                _, token, method = self.path.split("/", 2)
                if token != "bot" + GOOD:
                    return self._out(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                fake.calls.append((method, body))
                if method == "getMe":
                    return self._out(200, {"ok": True, "result": {"id": 1, "username": "law_test_bot"}})
                if method == "getUpdates":
                    off = body.get("offset")
                    ups = fake.updates
                    if off == -1:
                        ups = ups[-1:]
                    elif off is not None:
                        ups = [u for u in ups if u["update_id"] >= off]
                    return self._out(200, {"ok": True, "result": ups})
                if method == "sendMessage":
                    fake.next_msg += 1
                    return self._out(200, {"ok": True, "result": {"message_id": fake.next_msg}})
                return self._out(200, {"ok": True, "result": True})

            def _out(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def msg(self, text, uid=ME):
        u = {"update_id": self.next_update, "message": {"message_id": self.next_update, "text": text,
                                                         "chat": {"id": uid, "type": "private"},
                                                         "from": {"id": uid, "first_name": "Thaqif"}}}
        self.next_update += 1
        self.updates.append(u)
        return u

    def sent(self):
        return [b["text"] for m, b in self.calls if m == "sendMessage"]

    def uploads(self):
        return [m for m, b in self.calls if b.get("_multipart")]

    def edits(self):
        return [b["text"] for m, b in self.calls if m == "editMessageText"]


@pytest.fixture
def tg(home):
    f = FakeTG()
    yield f
    f.srv.shutdown()


@pytest.fixture
def bot(tg):
    w, _ = make_rt()
    b = Bot(w.paths, w.reg, w.q, Api(GOOD, base=tg.base), {ME}, cfg={"edit_every_s": 0})
    yield b, w, tg
    w.stop_event.set()
    w.mm.stop()


def drain(w, limit=20):
    n = 0
    while (job := w.q.claim()) is not None and n < limit:
        w.handle(job)
        n += 1


def say(b, tg, text, uid=ME):
    b.handle(tg.msg(text, uid))


# ── basics ──────────────────────────────────────────────────────────────
def test_creds_split_and_event_lines(home):
    p = get_paths()
    write_env(p.env_file, {"TELEGRAM_BOT_TOKEN": GOOD, "TELEGRAM_USER_ID": "4242, 77"})
    assert creds(p) == (GOOD, {4242, 77})
    assert beat_age(p) is None                              # never polled
    parts = split_text("line\n" * 2000, 4000)
    assert len(parts) == 3 and all(len(x) <= 4000 for x in parts)
    assert "🔧 write_file" in event_line("agent", {"step": 1, "tool": "write_file", "running": True, "args": "{}"})
    assert "loading qwen38-27b" in event_line("model", {"state": "loading", "model": "qwen38-27b", "gb": 16})
    assert "🧠" in event_line("agent", {"step": 2, "think": "weighing the options"})
    assert event_line("deltas", {}) is None


def test_md_to_html():
    from law.telegram import md_to_html
    assert md_to_html("**bold** and *it*") == "<b>bold</b> and <i>it</i>"
    assert "<code>du -ah</code>" in md_to_html("run `du -ah`")
    h = md_to_html("```\nline1\nline2\n```")
    assert "<pre>" in h and "line1" in h and "line2" in h
    assert md_to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"       # escaped, not formatted
    assert '<a href="https://x.com">link</a>' in md_to_html("[link](https://x.com)")
    assert md_to_html("# Title") == "<b>Title</b>"
    assert "<b>" not in md_to_html("2 * 3 * 4")                           # lone stars aren't italics


def test_media_edit_followup_reruns_with_the_tweak(bot):
    b, w, tg = bot
    say(b, tg, "make an image of a mountain lake at sunrise")
    j1 = w.q.jobs(1)[0]
    assert j1["kind"] == "image"
    say(b, tg, "make it more dramatic")                                   # short tweak → edit the last image
    j2 = w.q.jobs(1)[0]
    assert j2["kind"] == "image" and j2["id"] != j1["id"]
    p = json.loads(j2["payload"])["prompt"]
    assert "mountain lake" in p and "more dramatic" in p                  # tweak folded into the prompt


def test_live_split_and_tail():
    # before </think> everything streamed is still thinking; after it, the answer
    assert live_split("I should check the files") == ("I should check the files", "")
    assert live_split("thinking hard</think>\n\nThe answer is 42") == ("thinking hard", "The answer is 42")
    assert live_split("<think>plan</think>done") == ("plan", "done")
    assert tail("a b   c", 10) == "a b c"
    t = tail("x" * 50, 10)
    assert t.startswith("…") and len(t) == 11


def test_streamed_thinking_shows_live_and_typing(bot):
    b, w, tg = bot
    jid = w.q.enqueue("qwen38-27b", {"task": "t", "cwd": "/tmp/x",
                                     "messages": [{"role": "user", "content": "t"}]},
                      persona="general", source="telegram", kind="agent")
    b.follow(ME, jid, "agent", f"job {jid}")
    w.q.add_event(jid, "status", {"state": "running", "attempt": 1})
    w.q.add_event(jid, "deltas", [{"content": "Let me reason about "}, {"content": "the task"}])
    b.pump()
    assert any("🧠" in e and "reason about the task" in e for e in tg.edits())   # live thinking shown
    assert any(m == "sendChatAction" for m, _ in tg.calls)                       # "… is typing"
    w.q.add_event(jid, "deltas", [{"content": "</think>Here is the answer"}])
    b.pump()
    assert any("💬" in e and "Here is the answer" in e for e in tg.edits())      # live answer shown


def test_media_kind_and_multipart():
    from law.telegram import media_kind, multipart
    assert media_kind(Path("a/pic.PNG")) == ("sendPhoto", "photo")
    assert media_kind(Path("v.mp4")) == ("sendVideo", "video")
    assert media_kind(Path("s.wav")) == ("sendAudio", "audio")
    assert media_kind(Path("report.pdf")) == ("sendDocument", "document")
    ctype, body = multipart({"chat_id": 7, "caption": "hi"}, "photo", "x.png", b"\x89PNG\x00data")
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"' in body and b"\x89PNG\x00data" in body
    assert b'filename="x.png"' in body


def test_route_media_plain_language():
    from law.telegram import route_media
    k, p = route_media("make me an image of a sunset over the ocean")
    assert k == "image" and p["prompt"] == "a sunset over the ocean"
    k, p = route_media("generate a logo for a coffee shop")
    assert k == "image" and "coffee shop" in p["prompt"]
    k, p = route_media("create a short video of a dog running on a beach")
    assert k == "video" and p["prompt"] == "a dog running on a beach"
    k, p = route_media("say hello everyone, welcome to the show")
    assert k == "tts" and p["text"] == "hello everyone, welcome to the show"
    k, p = route_media("read this aloud: the quick brown fox")
    assert k == "tts" and p["text"] == "the quick brown fox"
    # ordinary agent tasks must NOT be routed to media generation
    assert route_media("create a function that returns an image buffer") is None
    assert route_media("write a blog post about image generation") is None
    assert route_media("fix the failing tests and commit") is None
    assert route_media("read the README and summarize it") is None


def test_is_question_vs_task():
    from law.telegram import is_question
    # questions → answered in chat
    for q in ["how can i find the biggest 20 files on my mac?",
              "what is the difference between tcp and udp",
              "is there a terminal command to list open ports?",
              "why does docker need a vm on macos?",
              "can you explain how the worker queue works?",
              "how do i deploy a next.js app?"]:
        assert is_question(q), q
    # tasks → run an agent
    for t in ["build me a todo app in react",
              "fix the failing tests and commit",
              "can you build an instagram clone in ~/sc/ig",
              "please deploy the app on port 3006",
              "refactor the worker to use asyncio",
              "add a dark mode toggle to the settings page"]:
        assert not is_question(t), t


def test_build_task_uses_the_orchestrator(bot):
    b, w, tg = bot
    say(b, tg, "build a twitter clone web app on port 3007")
    j = w.q.jobs(1)[0]
    assert j["kind"] == "agent" and j["persona"] == "orchestrator"      # delegates to coder/qa/etc.
    # a non-build task stays on the default persona
    say(b, tg, "rename all the variables in utils.py to snake_case")
    j2 = w.q.jobs(1)[0]
    assert j2["persona"] == "general"


def test_casual_message_after_a_task_is_chat_not_an_agent(bot):
    b, w, tg = bot
    say(b, tg, "build a hello world script")          # task → agent, sets last_mode=task
    assert w.q.jobs(1)[0]["kind"] == "agent"
    for casual in ("bro", "thanks!", "nice", "ok cool"):
        say(b, tg, casual)
        j = w.q.jobs(1)[0]
        assert j["kind"] == "chat", f"{casual!r} should be chat, got {j['kind']}"
    # but a substantive follow-up after a task still goes to the agent
    say(b, tg, "the script crashed with a syntax error on line 3, please fix it")
    assert w.q.jobs(1)[0]["kind"] == "agent"


def test_plain_question_is_answered_not_run_as_a_task(bot):
    b, w, tg = bot
    say(b, tg, "how can i find the biggest 20 files on my mac?")
    j = w.q.jobs(1)[0]
    assert j["kind"] == "chat" and j["source"] == "telegram"      # chat answer, NOT an agent task
    assert j.get("persona") not in ("orchestrator",)              # not a build persona


def test_conversation_followup_keeps_context(bot):
    b, w, tg = bot
    say(b, tg, "what command lists the biggest files on disk?")
    j1 = w.q.jobs(1)[0]
    assert j1["kind"] == "chat"
    w.q.claim()                                          # move it to running so it can finish
    w.q.finish(j1["id"], "done",
               result={"message": {"role": "assistant", "content": "use: du -ah . | sort -rh | head"}})
    b.pump()                                             # finish → records the answer in history
    say(b, tg, "it's not working")                       # vague follow-up, no task verb
    j2 = w.q.jobs(1)[0]
    assert j2["id"] != j1["id"] and j2["kind"] == "chat"  # stays a chat (follows the conversation)
    msgs = json.loads(j2["payload"])["messages"]
    assert any("biggest files" in m["content"] for m in msgs)    # prior question carried
    assert any("du -ah" in m["content"] for m in msgs)           # prior answer carried
    assert msgs[-1] == {"role": "user", "content": "it's not working"}


def test_followup_after_a_task_carries_context_to_the_agent(bot):
    b, w, tg = bot
    say(b, tg, "build a hello world script in python")    # task verb → agent
    j1 = w.q.jobs(1)[0]
    assert j1["kind"] == "agent"
    say(b, tg, "it printed nothing when i ran it")        # vague follow-up after a task → agent w/ context
    j2 = w.q.jobs(1)[0]
    assert j2["kind"] == "agent" and j2["id"] != j1["id"]
    task = json.loads(j2["payload"])["task"]
    assert "hello world script" in task and "it printed nothing" in task   # prior turn is in the brief


def test_info_question_is_grounded_with_web_search(bot, monkeypatch):
    b, w, tg = bot
    calls = []
    monkeypatch.setattr(b, "_web_lookup", lambda q, n=5: calls.append(q) or "- KL: 32C, thunderstorms (ex.com)")
    b.answer(ME, "what's the weather in kuala lumpur today?")
    m = json.loads(w.q.jobs(1)[0]["payload"])["messages"]
    assert m[0]["role"] == "system" and "Web search results" in m[0]["content"] and "32C" in m[0]["content"]
    assert calls                                              # a current-facts question triggers a lookup
    calls.clear()
    b.answer(ME, "explain how a hash map works")
    m2 = json.loads(w.q.jobs(1)[0]["payload"])["messages"]
    assert not calls                                          # general knowledge → no web lookup
    assert m2[0]["role"] == "system" and "Today is" in m2[0]["content"]   # but still dated


def test_plain_media_message_routes_to_generation(bot):
    b, w, tg = bot
    say(b, tg, "make an image of a neon tokyo street at night")
    j = w.q.jobs(1)[0]
    assert j["kind"] == "image" and j["source"] == "telegram"
    assert "neon tokyo street" in json.loads(j["payload"])["prompt"]


def test_photo_upload_is_routed_to_vision(bot, tmp_path, monkeypatch):
    b, w, tg = bot
    src = tmp_path / "pic.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    monkeypatch.setattr(b.api, "get_file", lambda fid: {"file_path": f"photos/{fid}.jpg"})

    def fake_dl(fp, dest, **k):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(src.read_bytes())
        return Path(dest)
    monkeypatch.setattr(b.api, "download_file", fake_dl)
    upd = {"update_id": 900, "message": {"message_id": 1, "chat": {"id": ME, "type": "private"},
           "from": {"id": ME}, "caption": "where is this place?",
           "photo": [{"file_id": "thumb"}, {"file_id": "full"}]}}
    b.handle(upd)
    j = w.q.jobs(1)[0]
    assert j["kind"] == "vision" and j["source"] == "telegram"
    p = json.loads(j["payload"])
    assert p["prompt"] == "where is this place?" and p["image"].endswith(".jpg")
    assert Path(p["image"]).is_file()                       # the photo was downloaded and saved


def test_voice_note_is_transcribed_then_routed(bot, tmp_path, monkeypatch):
    b, w, tg = bot
    src = tmp_path / "v.oga"
    src.write_bytes(b"OggS\x00voicebytes")
    monkeypatch.setattr(b.api, "get_file", lambda fid: {"file_path": f"voice/{fid}.oga"})

    def fake_dl(fp, dest, **k):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(src.read_bytes())
        return Path(dest)
    monkeypatch.setattr(b.api, "download_file", fake_dl)
    upd = {"update_id": 950, "message": {"message_id": 1, "chat": {"id": ME, "type": "private"},
           "from": {"id": ME}, "voice": {"file_id": "v1", "duration": 3}}}
    b.handle(upd)
    j = w.q.jobs(1)[0]
    assert j["kind"] == "stt" and j["source"] == "telegram"
    assert Path(json.loads(j["payload"])["audio"]).is_file()           # the voice note was downloaded
    # simulate transcription finishing → the spoken request routes like a typed message
    w.q.claim()
    w.q.finish(j["id"], "done", result={"message": {"role": "assistant",
                                                     "content": "generate an image of a red sports car"}})
    b.pump()
    j2 = w.q.jobs(1)[0]
    assert j2["kind"] == "image"                                        # spoken "generate an image" → image gen
    assert "red sports car" in json.loads(j2["payload"])["prompt"]


def test_image_command_generates_and_sends_the_photo(bot, tmp_path):
    b, w, tg = bot
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n" + b"img-bytes" * 50)
    say(b, tg, "/image a neon city at night")
    jid = w.q.jobs(1)[0]["id"]
    assert w.q.get(jid)["kind"] == "image"                       # routed to the image pipeline
    # simulate the worker finishing the image job with a saved PNG
    w.q.claim()
    w.q.finish(jid, "done", result={"image": {"path": str(png), "seconds": 4, "model": "z-image-turbo"}})
    b.pump()
    assert "sendPhoto" in tg.uploads()                           # the actual photo was delivered
    # the verbose "...: /path" text answer is NOT sent for a media job
    assert not any(str(png) in s for s in tg.sent())


def test_voice_and_video_commands_route_to_the_right_kind(bot):
    b, w, tg = bot
    say(b, tg, "/voice am_adam hello there friend")
    say(b, tg, "/video a dog running on a beach")
    kinds = {j["kind"] for j in w.q.jobs(5)}
    assert "tts" in kinds and "video" in kinds
    tts = next(j for j in w.q.jobs(5) if j["kind"] == "tts")
    assert json.loads(tts["payload"])["params"]["voice"] == "am_adam"


def test_send_file_agent_tool_delivers_over_telegram(bot, tmp_path, monkeypatch):
    from law.agent.tools import ToolCtx, execute
    b, w, tg = bot
    write_env(w.paths.env_file, {"TELEGRAM_BOT_TOKEN": GOOD, "TELEGRAM_USER_ID": str(ME)})
    monkeypatch.setenv("LAW_TELEGRAM_API", tg.base)        # the tool builds its own Api → fake it
    f = tmp_path / "out.png"
    f.write_bytes(b"\x89PNG\r\n" + b"x" * 100)
    ctx = ToolCtx(project=tmp_path, telegram_chat_id=ME, paths=w.paths,
                  cfg={"tool_output_chars": 2000})
    out = execute("send_file", {"path": "out.png", "caption": "here you go"}, ctx)
    assert out.startswith("sent out.png") and "sendPhoto" in tg.uploads()
    # no chat → a clear, non-crashing message
    ctx2 = ToolCtx(project=tmp_path, paths=w.paths, cfg={})
    assert execute("send_file", {"path": "out.png"}, ctx2).startswith("ERROR: send_file only works")


def test_dashboard_snapshot_and_events(home):
    from law.runtime import dashboard
    w, _ = make_rt()
    jid = w.q.enqueue("qwen38-27b", {"task": "hello world", "cwd": "/tmp/x",
                                     "messages": [{"role": "user", "content": "hi"}]},
                      persona="general", source="cli", kind="agent")
    w.q.add_event(jid, "agent", {"step": 1, "think": "pondering"})
    snap = dashboard.snapshot(w.paths, w.reg, w.q)
    assert snap["machine"]["ram_gb"] >= 1 and snap["mem"]["total_gb"] >= 1
    assert any(j["id"] == jid and j["prompt"] == "hello world" for j in snap["jobs"])
    assert any(p["name"] == "dashboard" for p in snap["ports"])
    assert dashboard.job_prompt({"payload": json.dumps({"task": "do   a\n thing"})}) == "do a thing"
    evs = w.q.recent_events(0.0)
    assert any(e["job_id"] == jid and e["type"] == "agent" for e in evs)
    assert "Local AI Workstation" in dashboard.PAGE and "api/events" in dashboard.PAGE
    assert "renderKanban" in dashboard.PAGE and "openJob" in dashboard.PAGE        # Jira-style board + drawer
    for v in ("view-dashboard", "view-chat", "view-team", "view-models", "view-studio"):
        assert v in dashboard.PAGE                                                 # multi-view app, not one page
    assert "/api/vision" in dashboard.PAGE and "analyseImage" in dashboard.PAGE    # vision upload panel
    assert any(m["id"] for m in snap["models"])                                    # models listed for the Models view
    # job-detail ("ticket") endpoint used by the drawer
    det = dashboard.Handler._job_detail.__get__(type("H", (), {"q": w.q})())  # bound to a stub with .q
    d = det(jid)
    assert d["id"] == jid and d["prompt"] == "hello world"
    assert any(ev["k"] == "think" for ev in d["timeline"])                         # thinking in the timeline
    w.stop_event.set()
    w.mm.stop()


def test_dashboard_vision_endpoint_enqueues(home, monkeypatch):
    import base64
    from law.runtime import dashboard, visiongen
    w, _ = make_rt()
    monkeypatch.setattr(visiongen, "available", lambda p: True)       # pretend mlx-vlm is installed
    H = type("H", (), {"q": w.q, "paths": w.paths})()
    b64 = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n" + b"x" * 40).decode()
    res = dashboard.Handler._enqueue_vision.__get__(H)({"image_b64": b64, "ext": "png",
                                                        "prompt": "where is this?"})
    assert res.get("ok") and res.get("id")
    j = w.q.get(res["id"])
    assert j["kind"] == "vision"
    p = json.loads(j["payload"])
    assert p["prompt"] == "where is this?" and Path(p["image"]).is_file()           # saved the upload
    assert dashboard.Handler._enqueue_vision.__get__(H)({"image_b64": ""}).get("error")  # no image → error
    w.stop_event.set()
    w.mm.stop()


def test_reminders_and_daily_schedules(bot):
    b, w, tg = bot
    say(b, tg, "/remind in 30m drink water")
    say(b, tg, "/daily 08:00 summarise the news")
    scheds = [s for s in b.state["schedules"] if s["chat"] == ME]
    assert len(scheds) == 2 and {s["kind"] for s in scheds} == {"once", "daily"}
    say(b, tg, "/schedules")
    assert any("drink water" in s and "summarise the news" in s for s in [tg.sent()[-1]])
    # make the one-off due → it fires (sends the reminder) and is removed
    for s in b.state["schedules"]:
        if s["kind"] == "once":
            s["at"] = 0.0
    b._due_schedules()
    assert any("Reminder: drink water" in s for s in tg.sent())
    assert not any(s["kind"] == "once" for s in b.state["schedules"])
    did = [s["id"] for s in b.state["schedules"] if s["kind"] == "daily"][0]
    say(b, tg, f"/unschedule {did}")
    assert not b.state["schedules"]


def test_dashboard_usage_endpoint(home):
    from law.runtime import dashboard
    w, _ = make_rt()
    w.q.enqueue("qwen38-27b", {"messages": [{"role": "user", "content": "hi"}]}, kind="chat", source="cli")
    H = type("H", (), {"q": w.q, "paths": w.paths})()
    u = dashboard.Handler._usage.__get__(H)()
    assert u["total"] >= 1 and "by_kind" in u and "by_day" in u and "compute_s" in u
    assert "view-usage" in dashboard.PAGE and "/api/usage" in dashboard.PAGE and "renderUsage" in dashboard.PAGE
    assert "view-gallery" in dashboard.PAGE and "/api/gallery" in dashboard.PAGE and "renderGallery" in dashboard.PAGE
    g = dashboard.Handler._gallery.__get__(H)()
    assert "items" in g and isinstance(g["items"], list)           # gallery endpoint returns a list
    w.stop_event.set()
    w.mm.stop()


def test_dashboard_is_a_managed_service(home):
    from law.steps.services import SERVICES, wanted, label
    from law.engine import Ctx
    from law.system import System
    assert SERVICES.get("dashboard") == "law.runtime.dashboard"
    ctx = Ctx(paths=get_paths(), sys=System())
    assert label("dashboard") in wanted(ctx)


def test_strangers_are_refused_once_and_ignored(bot):
    b, w, tg = bot
    say(b, tg, "rm everything", uid=999)
    say(b, tg, "again", uid=999)
    assert w.q.jobs(5) == []
    msgs = tg.sent()
    assert len(msgs) == 1 and "private" in msgs[0] and "999" in msgs[0]


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_plain_message_runs_an_agent_and_undo_rolls_it_back(bot):
    b, w, tg = bot
    say(b, tg, "please WRITE a file")
    job = w.q.jobs(1)[0]
    assert job["kind"] == "agent" and job["source"] == "telegram" and job["persona"] == "general"
    project = get_paths().source / "telegram"
    assert json.loads(job["payload"])["cwd"] == str(project)
    assert "queued" in tg.sent()[-1] and f"job {job['id']}" in tg.sent()[-1]
    drain(w)
    b.pump()
    assert (project / "hello.txt").exists()
    assert any("FINAL: created" in s for s in tg.sent())
    assert any("/undo" in s for s in tg.sent())
    assert any("✅ done" in e and "tool calls" in e for e in tg.edits())
    assert b.state["follows"] == {}
    say(b, tg, "/undo")
    assert not (project / "hello.txt").exists()
    assert "restored" in tg.sent()[-1]


def test_progress_message_is_edited_while_running(bot):
    b, w, tg = bot
    say(b, tg, "please WRITE it")
    job = w.q.claim()
    w.handle(job)                                           # finishes; pump sees all events at once
    f = b.state["follows"][str(job["id"])]
    f_before = dict(f)
    b.pump()
    assert f_before["msg"] is not None
    assert any("snapshot" in e or "done" in e for e in tg.edits())


def test_ask_gives_a_plain_answer(bot):
    b, w, tg = bot
    say(b, tg, "/ask hi")
    job = w.q.jobs(1)[0]
    assert job["kind"] == "chat"
    drain(w)
    b.pump()
    assert tg.sent()[-1] == "Hello from qwen38-27b"


def test_plan_then_go(bot):
    b, w, tg = bot
    say(b, tg, "/plan please WRITE a file")
    plan_job = w.q.jobs(1)[0]
    assert json.loads(plan_job["payload"])["plan_only"] is True
    drain(w)
    b.pump()
    assert any("Task: Test task" in s for s in tg.sent())    # spec is sent (now as rich HTML: # → <b>)
    assert "/go" in tg.sent()[-1]
    assert not (get_paths().source / "telegram" / "hello.txt").exists()     # nothing ran
    st = w.q.get_state(plan_job["id"])
    assert st["snapshot"] is None
    say(b, tg, "/go")
    go = w.q.jobs(1)[0]
    p = json.loads(go["payload"])
    assert p["translate"] is False and p["spec_text"].startswith("# Task: Test task")
    drain(w)
    b.pump()
    assert (get_paths().source / "telegram" / "hello.txt").exists()
    assert Store(get_paths().agent_db).get_spec(get_paths().source / "telegram", go["id"]) is not None
    say(b, tg, "/go")
    assert "No plan waiting" in tg.sent()[-1]


def test_raw_skips_the_translator(bot):
    b, w, tg = bot
    say(b, tg, "/raw please WRITE it")
    assert json.loads(w.q.jobs(1)[0]["payload"])["translate"] is False


def test_cancel_and_resume_default_to_the_last_job(bot):
    b, w, tg = bot
    say(b, tg, "please WRITE it")
    jid = w.q.jobs(1)[0]["id"]
    say(b, tg, "/cancel")
    assert w.q.status(jid) == "cancelled" and "cancelled" in tg.sent()[-1]
    b.pump()
    assert any("⏹ cancelled" in e for e in tg.edits())
    say(b, tg, "/resume")
    assert w.q.status(jid) == "queued"
    drain(w)
    b.pump()
    assert w.q.status(jid) == "done"
    say(b, tg, "/cancel 99999")
    assert "not running" in tg.sent()[-1]


def test_failed_job_offers_resume(bot):
    b, w, tg = bot
    b.chat(ME)["project"] = str(get_paths().home)            # forbidden on purpose (set directly)
    say(b, tg, "WRITE")
    assert "Can't use that project folder" in tg.sent()[-1]
    jid = w.q.enqueue("qwen38-27b", {"task": "x", "cwd": str(get_paths().home), "depth": 0,
                                     "messages": [{"role": "user", "content": "x"}]}, kind="agent")
    b.follow(ME, jid, "agent", f"job {jid}")
    drain(w)
    b.pump()
    assert f"/resume {jid}" in tg.sent()[-1]


def test_persona_project_status_jobs_help(bot):
    b, w, tg = bot
    say(b, tg, "/persona")
    assert "coder" in tg.sent()[-1]
    say(b, tg, "/persona nope")
    assert "Unknown persona" in tg.sent()[-1]
    say(b, tg, "/persona coder")
    say(b, tg, "/project myapp")
    assert str(get_paths().source / "myapp") in tg.sent()[-1]
    say(b, tg, "/project ~")
    assert "refusing" in tg.sent()[-1]
    assert b.chat(ME)["project"] == "myapp"
    say(b, tg, "please WRITE it")
    job = w.q.jobs(1)[0]
    assert job["persona"] == "coder" and job["model"] == "qwen3-coder-next"
    assert json.loads(job["payload"])["cwd"] == str(get_paths().source / "myapp")
    say(b, tg, "/status")
    assert "worker" in tg.sent()[-1] and "memory" in tg.sent()[-1]
    say(b, tg, "/jobs")
    assert str(job["id"]) in tg.sent()[-1]
    say(b, tg, "/help")
    assert "/plan" in tg.sent()[-1]
    say(b, tg, "/frobnicate")
    assert "Unknown command" in tg.sent()[-1]


def test_polling_offset_backlog_and_restart(bot):
    b, w, tg = bot
    tg.msg("old message from before the bot existed")
    b.skip_backlog()
    assert b.poll_once(0) == 0 and w.q.jobs(5) == []            # backlog ignored
    tg.msg("/ask hi")
    assert b.poll_once(0) == 1 and len(w.q.jobs(5)) == 1
    assert b.poll_once(0) == 0                                  # offset saved → not handled twice
    b2 = Bot(w.paths, w.reg, w.q, Api(GOOD, base=tg.base), {ME})
    assert b2.poll_once(0) == 0 and str(w.q.jobs(1)[0]["id"]) in b2.state["follows"]


# ── services, watchdog ──────────────────────────────────────────────────
def test_telegram_service_only_when_configured(home):
    ctx = Ctx(paths=get_paths(), sys=FakeSystem(get_paths().hf_cache))
    assert label("telegram") not in wanted(ctx)
    write_env(get_paths().env_file, {"TELEGRAM_BOT_TOKEN": GOOD, "TELEGRAM_USER_ID": str(ME)})
    w1 = wanted(ctx)
    assert label("telegram") in w1 and "law.telegram" in w1[label("telegram")][1]
    write_env(get_paths().env_file, {"TELEGRAM_BOT_TOKEN": "123:OTHER", "TELEGRAM_USER_ID": str(ME)})
    assert wanted(ctx)[label("telegram")][1] != w1[label("telegram")][1]   # new token → restart
    assert GOOD not in w1[label("telegram")][1]                             # secret never in the plist


def test_watchdog_restarts_a_frozen_bot(home):
    restarts = []
    age = {"v": None}
    wd = Watchdog(load(get_paths()), Queue(get_paths().queue_db), restart=restarts.append, health=lambda: True,
                  telegram_age=lambda: age["v"])
    assert wd.check() == []
    age["v"] = 100
    assert wd.check() == []
    age["v"] = 1000
    assert wd.check() == ["restart telegram"] and restarts == ["com.law.telegram"]


def test_beat_age(home):
    p = get_paths()
    write_env(p.env_file, {"TELEGRAM_BOT_TOKEN": GOOD, "TELEGRAM_USER_ID": str(ME)})
    beat_file(p).parent.mkdir(parents=True, exist_ok=True)
    beat_file(p).write_text("x")
    assert beat_age(p) < 5


# ── law configure ───────────────────────────────────────────────────────
def ns(**kw):
    base = dict(telegram_token=None, telegram_user=None, hf_token=None, telegram_off=False, no_restart=True)
    return argparse.Namespace(**{**base, **kw})


def test_configure_non_interactive(tg, capsys):
    from law.util import read_env
    fac = lambda t: Api(t, base=tg.base)
    assert setupcmds.cmd_configure(ns(telegram_token=GOOD, telegram_user=str(ME), hf_token="hf_x"),
                                   api_factory=fac) == 0
    env = read_env(get_paths().env_file)
    assert env["TELEGRAM_BOT_TOKEN"] == GOOD and env["TELEGRAM_USER_ID"] == str(ME) and env["HF_TOKEN"] == "hf_x"
    assert "@law_test_bot" in capsys.readouterr().out
    assert setupcmds.cmd_configure(ns(telegram_token="999:BAD"), api_factory=fac) == 1
    assert setupcmds.cmd_configure(ns(telegram_user="me"), api_factory=fac) == 2
    assert setupcmds.cmd_configure(ns(telegram_off=True), api_factory=fac) == 0
    env = read_env(get_paths().env_file)
    assert "TELEGRAM_BOT_TOKEN" not in env and env["HF_TOKEN"] == "hf_x"


def test_configure_interactive_finds_the_user_id(tg):
    fac = lambda t: Api(t, base=tg.base)
    tg.msg("hello bot", uid=4242)
    answers = iter(["", "", "1"])
    secrets = iter([GOOD, ""])
    out = io.StringIO()
    ch = setupcmds.interactive(ask=lambda _: next(answers), secret=lambda _: next(secrets),
                               api_factory=fac, out=out)
    assert ch == {"TELEGRAM_BOT_TOKEN": GOOD, "TELEGRAM_USER_ID": "4242"}
    assert "4242" in out.getvalue() and "@law_test_bot" in out.getvalue()


# ── OpenCode ────────────────────────────────────────────────────────────
def test_opencode_provider_merges_and_keeps_your_settings(home):
    p = get_paths()
    reg = load(p)
    f = opencode.config_file(p.home)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"theme": "dark", "model": "anthropic/x", "provider": {"other": {"a": 1}}}))
    changed, want = opencode.needs_update(p.home, reg)
    assert changed
    assert want["theme"] == "dark" and want["model"] == "anthropic/x" and want["provider"]["other"] == {"a": 1}
    law_p = want["provider"]["law"]
    assert law_p["options"]["baseURL"] == "http://127.0.0.1:4000/v1"
    assert law_p["models"]["coder"]["limit"]["context"] == reg.models["qwen3-coder-next"].context
    assert "vision" in law_p["models"] and "embed" not in str(law_p["models"])
    opencode.write(p.home, want)
    assert f.with_name("opencode.json.law-backup").exists()
    assert opencode.needs_update(p.home, reg)[0] is False


def test_opencode_step_only_when_used_and_never_touches_jsonc(home, monkeypatch):
    # isolate from the host: whether `opencode` is on the dev machine's PATH must not
    # decide this test — "in use" here means only the sandbox home's config dir exists.
    monkeypatch.setattr(opencode, "which", lambda _name: None)
    p = get_paths()
    ctx = Ctx(paths=p, sys=FakeSystem(p.hf_cache))
    assert oc_step.plan_opencode(ctx) == []                 # OpenCode not used → nothing
    d = opencode.config_dir(p.home)
    d.mkdir(parents=True)
    (d / "opencode.jsonc").write_text('{ // mine\n "theme": "x" }')
    assert oc_step.plan_opencode(ctx) == []                 # commented config → left alone
    (d / "opencode.jsonc").unlink()
    acts = oc_step.plan_opencode(ctx)
    assert len(acts) == 1
    acts[0].fn()
    data = json.loads(opencode.config_file(p.home).read_text())
    assert data["model"] == "law/coder" and "law" in data["provider"]
    assert oc_step.plan_opencode(ctx) == []                 # converged


def test_setup_opencode_print(home, capsys):
    assert setupcmds.cmd_setup_opencode(argparse.Namespace(print=True, install=False)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"] == "law/coder" and "coder" in out["provider"]["law"]["models"]


# ── chat requests with tools: whole reply + recovered text calls ────────
@pytest.fixture
def gw(home):
    w, _ = make_rt()
    th = threading.Thread(target=w.run_forever, daemon=True)
    th.start()
    port = free_port()
    srv = Gateway(w.paths, w.reg, w.q).serve("127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", w
    srv.shutdown(); w.stop_event.set(); th.join(10)


TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {"type": "object"}}}]


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=30)


@pytest.mark.parametrize("kw", ["WRITE", "TEXTCALL", "XMLCALL"])
def test_openai_stream_with_tools_gets_clean_tool_calls(gw, kw):
    base, w = gw
    resp = _post(base + "/v1/chat/completions", {"model": "coder", "stream": True, "tools": TOOLS,
                                                 "messages": [{"role": "user", "content": f"{kw} hello"}]})
    calls, finish = [], None
    for raw in resp:
        line = raw.decode().strip()
        if line == "data: [DONE]":
            break
        if line.startswith("data: "):
            for ch in json.loads(line[6:]).get("choices", []):
                calls += ch["delta"].get("tool_calls") or []
                finish = ch.get("finish_reason") or finish
    assert len(calls) == 1 and calls[0]["function"]["name"] == "write_file"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "hello.txt", "content": "hi there"}
    assert finish == "tool_calls" and calls[0]["id"]


def test_anthropic_with_tools_gets_tool_use(gw):
    base, _ = gw
    req = urllib.request.Request(base + "/v1/messages", data=json.dumps({
        "model": "claude-sonnet-4-5", "max_tokens": 100,
        "tools": [{"name": "write_file", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "TEXTCALL"}]}).encode(), headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    tu = [c for c in r["content"] if c["type"] == "tool_use"]
    assert tu and tu[0]["name"] == "write_file" and tu[0]["input"]["path"] == "hello.txt"
    assert r["stop_reason"] == "tool_use"


def test_cli_legacy_flags_now_available(home, capsys):
    from law import cli
    assert cli.main(["--telegram"]) == 0
    assert "not configured" in capsys.readouterr().out
    assert cli.main(["--setup-opencode", "--print"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["--vision"])
