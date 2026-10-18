"""Phase 4a tests: agent tools, hard-block list, agent jobs end-to-end (snapshot, tool
loop, translator spec, delegation to sub-jobs, cancel/resume, memory, max steps), CLI —
plus the 4a.1 fixes for real local models: whole (non-streamed) agent turns, thinking that
leaks into the answer, tool calls written as text, nudging an agent that does nothing,
translator retry, saved raw replies."""
from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from law import cli
from law.agent import prompts
from law.agent.runner import compact, normalize, shrink_hard
from law.agent.textcalls import parse_text_calls, split_think, strip_calls
from law.agent.tools import ToolCtx, blocked, cap, execute
from law.paths import get_paths
from law.registry import load
from law.runtime.store import Store
from law.undo import Undo


def store() -> Store:
    return Store(get_paths().agent_db)

from test_phase1 import free_port
from test_phase1 import home  # noqa: F401  (fixture)
from test_phase2 import make_rt

GIT = shutil.which("git") is not None
NAMES = {"write_file", "run_shell", "read_file", "delegate"}


# ── tools ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", ["rm -rf /", "sudo rm -rf /", "rm -rf ~", "rm -rf ~/", "rm -fr $HOME",
                                 "rm -r -f /", "rm -rf /*", "diskutil eraseDisk APFS X disk2",
                                 "diskutil partitionDisk disk2 1 GPT", "mkfs.ext4 /dev/sda1",
                                 "dd if=/dev/zero of=/dev/disk2 bs=1m", "cat x > /dev/rdisk3",
                                 ":(){ :|:& };:", "cd /tmp && rm -rf / ; echo"])
def test_hard_block_catches_disasters(cmd):
    assert blocked(cmd)


@pytest.mark.parametrize("cmd", ["rm -rf ./build", "rm -rf /tmp/x", "rm -rf ~/MLX-AI/source/old",
                                 "rm file.txt", "git rm -r --cached x", "diskutil list", "dd if=a of=b",
                                 "docker rm -f web", "echo rm -rf"])
def test_hard_block_allows_normal_work(cmd):
    assert not blocked(cmd)


def test_file_tools(tmp_path):
    ctx = ToolCtx(project=tmp_path, cfg={"tool_output_chars": 1000})
    assert "created" in execute("write_file", {"path": "a/b.txt", "content": "one\ntwo\nthree"}, ctx)
    assert "two" in execute("read_file", {"path": "a/b.txt", "start_line": 2, "max_lines": 1}, ctx)
    assert "edited" in execute("edit_file", {"path": "a/b.txt", "old": "two", "new": "2"}, ctx)
    assert (tmp_path / "a/b.txt").read_text() == "one\n2\nthree"
    (tmp_path / "d.txt").write_text("x x")
    assert "appears 2 times" in execute("edit_file", {"path": "d.txt", "old": "x", "new": "y"}, ctx)
    assert "edited" in execute("edit_file", {"path": "d.txt", "old": "x", "new": "y", "replace_all": True}, ctx)
    assert "not found" in execute("edit_file", {"path": "d.txt", "old": "zzz", "new": "y"}, ctx)
    listing = execute("list_dir", {}, ctx)
    assert "a/" in listing and "b.txt" in listing
    assert execute("read_file", {"path": "nope.txt"}, ctx).startswith("ERROR")
    assert execute("nope", {}, ctx).startswith("ERROR: unknown tool")
    assert execute("read_file", {"wrong": 1}, ctx).startswith("ERROR: wrong arguments")
    (tmp_path / "bin.dat").write_bytes(b"\0\1\2")
    assert "binary" in execute("read_file", {"path": "bin.dat"}, ctx)


def test_send_email_tool_validates(tmp_path):
    ctx = ToolCtx(project=tmp_path, cfg={"tool_output_chars": 2000})
    assert execute("send_email", {"to": "not-an-email", "subject": "x"}, ctx).startswith("ERROR")
    assert "does not exist" in execute("send_email",
                                       {"to": "a@b.com", "subject": "x", "attachment": "nope.pdf"}, ctx)
    # schema is registered
    from law.agent.tools import SCHEMAS
    assert any(s["function"]["name"] == "send_email" for s in SCHEMAS)


def test_look_at_tool(tmp_path, monkeypatch):
    from law.runtime import visiongen
    monkeypatch.setattr(visiongen, "available", lambda p: True)
    monkeypatch.setattr(visiongen, "describe",
                        lambda paths, image, q, **k: {"text": "a clean blue login form, well spaced"})
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\nfakepng")
    ctx = ToolCtx(project=tmp_path, paths=object(), cfg={"tool_output_chars": 2000})
    out = execute("look_at", {"image": "shot.png", "question": "assess the UI"}, ctx)
    assert "blue login form" in out and "looked at shot.png" in out
    assert execute("look_at", {"image": "missing.png"}, ctx).startswith("ERROR")   # no file → clear error


def test_search_files_tool(tmp_path):
    ctx = ToolCtx(project=tmp_path, cfg={"tool_output_chars": 4000})
    (tmp_path / "app.py").write_text("def handler():\n    return TODO_fix_me\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "page.tsx").write_text("const x = 1\n// TODO: style this\n")
    (tmp_path / "notes.md").write_text("plain notes, nothing here\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("TODO should be skipped\n")
    (tmp_path / "pic.png").write_bytes(b"\0\1\2TODO")
    # finds matches across text files, with file:line: prefix, skipping node_modules + binaries
    out = execute("search_files", {"pattern": "TODO"}, ctx)
    assert "app.py:2:" in out and "web/page.tsx:2:" in out
    assert "node_modules" not in out and "pic.png" not in out
    # glob narrows to a filetype
    only_tsx = execute("search_files", {"pattern": "TODO", "glob": "*.tsx"}, ctx)
    assert "page.tsx" in only_tsx and "app.py" not in only_tsx
    # case-insensitive + no-match + bad-regex paths
    assert "page.tsx" in execute("search_files", {"pattern": "todo", "ignore_case": True}, ctx)
    assert "no matches" in execute("search_files", {"pattern": "zzz_absent"}, ctx)
    assert execute("search_files", {"pattern": "("}, ctx).startswith("ERROR: invalid regex")
    assert execute("search_files", {"pattern": "  "}, ctx).startswith("ERROR")


def test_shell_tool_runs_in_project_and_caps_output(tmp_path):
    ctx = ToolCtx(project=tmp_path, cfg={"tool_output_chars": 200, "shell_timeout_s": 30})
    out = execute("run_shell", {"command": "pwd; echo hi; exit 3"}, ctx)
    assert "exit code 3" in out and str(tmp_path) in out and "hi" in out
    long = execute("run_shell", {"command": "python3 -c \"print('x'*5000)\""}, ctx)
    assert "characters cut" in long
    t = execute("run_shell", {"command": "sleep 5", "timeout_s": 0.5}, ctx)
    assert "timed out" in t
    r = execute("run_shell", {"command": "rm -rf /"}, ctx)
    assert r.startswith("REFUSED")


def test_web_tools(tmp_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/search"):
                body = json.dumps({"results": [{"title": "Law docs", "url": "http://x/doc",
                                                "content": "local ai"}]}).encode()
                ctype = "application/json"
            else:
                body = b"<html><head><style>x{}</style></head><body><h1>Hello</h1><p>World &amp; more</p>" \
                       b"<script>bad()</script></body></html>"
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ctx = ToolCtx(project=tmp_path, searxng_port=port)
        assert "Law docs" in execute("web_search", {"query": "law"}, ctx)
        page = execute("web_fetch", {"url": f"http://127.0.0.1:{port}/page"}, ctx)
        assert "Hello" in page and "World & more" in page and "bad()" not in page and "x{}" not in page
        assert execute("web_fetch", {"url": "file:///etc/passwd"}, ctx).startswith("ERROR")
        down = ToolCtx(project=tmp_path, searxng_port=free_port())
        assert "SearXNG" in execute("web_search", {"query": "x"}, down)
    finally:
        srv.shutdown()


def test_cap_and_compact():
    assert cap("abc", 10) == "abc"
    assert "cut" in cap("x" * 100, 20)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    msgs += [{"role": "tool", "content": "y" * 5000} for _ in range(10)]
    msgs += [{"role": "assistant", "content": "a"}] * 6
    compact(msgs, 20000)
    assert sum(len(m["content"]) for m in msgs) <= 20000


def test_shrink_hard_bounds_a_runaway_conversation():
    # the recovery when even a compacted prompt was too slow to read (handover §6.1)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for _ in range(6):               # distinct dicts (not shared refs) so trimming one is independent
        msgs.append({"role": "assistant", "content": "a" * 3000, "tool_calls": [{"id": "1"}]})
        msgs.append({"role": "tool", "content": "z" * 9000})
    before = sum(len(str(m.get("content") or "")) for m in msgs)
    removed = shrink_hard(msgs, 24000)                                          # context_chars//2 in real use
    assert removed > 0
    assert sum(len(str(m.get("content") or "")) for m in msgs) <= 24000 < before
    assert msgs[0]["content"] == "s" and msgs[1]["content"] == "u"              # never the system/task
    assert msgs[-1]["content"] == "z" * 9000                                    # the latest result stays full


def test_store_prune_keeps_recent_jobs_and_never_memory(tmp_path):
    s = Store(tmp_path / "a.db")
    proj = tmp_path / "proj"
    for jid in range(1, 11):
        s.put_spec(proj, jid, f"spec {jid}")
        s.put_run(proj, jid, "step-1", f"reply {jid}")
    s.add_memory(proj, "the api base is http://mac:4000")
    removed = s.prune(keep_jobs=3, keep_days=0)
    assert removed["specs"] == 7 and removed["runs"] == 7
    assert s.spec_count(proj) == 3
    assert s.get_spec(proj, 10) and s.get_spec(proj, 8) and not s.get_spec(proj, 7)
    assert s.memory_markdown(proj)                                              # durable facts untouched


def test_orchestrator_is_told_to_delegate(tmp_path):
    orch = prompts.system_prompt("orchestrator", tmp_path, can_delegate=True,
                                 personas=["orchestrator", "coder", "qa"])
    assert "MUST delegate" in orch and "implement it yourself" in orch          # strong, not optional
    coder = prompts.system_prompt("coder", tmp_path, can_delegate=True, personas=["orchestrator", "coder"])
    assert "MUST delegate" not in coder                                         # specialists keep the soft rule
    spec = {"title": "App", "goal": "build and test an app", "deliverables": ["app", "tests"]}
    p_orch = prompts.spec_to_prompt(spec, "build it", persona="orchestrator")
    assert "Do NOT build this yourself" in p_orch and "delegate" in p_orch.lower()
    p_coder = prompts.spec_to_prompt(spec, "build it", persona="coder")
    assert "create files with write_file" in p_coder                            # specialists still do the work
    assert "REAL RESULT A USER WOULD SEE" in p_coder and "HTTP 200" in p_coder   # verify rendered output, not just 200
    assert "renders WITH" in prompts.ROLES["coder"]                             # same rule for raw/delegated tasks


def test_quality_loop_screenshot_and_po():
    from law.agent.tools import SCHEMAS, FUNCS
    from law.registry import load
    assert "screenshot" in FUNCS and any(s["function"]["name"] == "screenshot" for s in SCHEMAS)
    reg = load()
    assert reg.personas.get("po", {}).get("model")                              # product-owner persona exists
    # the review loop is the DEFAULT for code/app work, but not for a plain doc task
    code = prompts.spec_to_prompt({"goal": "build a web app", "deliverables": ["app"]}, "x", persona="orchestrator")
    assert "QUALITY LOOP" in code and "po (product owner)" in code and "ACCEPT" in code
    doc = prompts.spec_to_prompt({"goal": "write a poem", "deliverables": ["poem"]}, "x", persona="orchestrator")
    assert "QUALITY LOOP" not in doc
    assert "ACCEPT" in prompts.ROLES["po"] and "screenshot" in prompts.ROLES["qa"]


def test_prompts_and_spec_parsing(tmp_path):
    (tmp_path / ".local-agent" / "memory").mkdir(parents=True)
    from law.project import AGENT_MD
    (tmp_path / ".local-agent" / "AGENT.md").write_text(AGENT_MD)
    (tmp_path / ".local-agent" / "memory" / "MEMORY.md").write_text("# Agent Memory Index\n\nNo memories yet.\n")
    sp = prompts.system_prompt("coder", tmp_path, can_delegate=False, personas=["coder"])
    assert "Project instructions" not in sp and "Project memory" not in sp      # untouched templates skipped
    (tmp_path / ".local-agent" / "AGENT.md").write_text(AGENT_MD + "\nAlways use port 9123 for the app.\n")
    sp = prompts.system_prompt("coder", tmp_path, can_delegate=True, personas=["coder", "qa"])
    assert "port 9123" in sp and "delegate" in sp and "senior software engineer" in sp
    spec = prompts.parse_spec('<think>hmm</think> sure: {"title":"T","goal":"G","steps":"one"}')
    assert spec["goal"] == "G" and spec["steps"] == ["one"] and spec["assumptions"] == []
    assert prompts.parse_spec("no json here") is None
    text = prompts.spec_to_prompt(spec, "do the thing")
    assert '"do the thing"' in text and "Definition of done" in text


# ── 4a.1: what real local models send back ──────────────────────────────
def test_split_think_handles_leaked_thinking():
    assert split_think("Simple task. Create the file.\n</think>\n\n") == ("", "Simple task. Create the file.")
    assert split_think("<think>plan</think>Done.") == ("Done.", "plan")
    assert split_think("All good.", "r") == ("All good.", "r")


def test_parse_text_calls_all_formats():
    j = parse_text_calls('ok <tool_call>{"name": "write_file", "arguments": {"path": "a", "content": "b"}}'
                         '</tool_call>', NAMES)
    assert j[0]["function"]["name"] == "write_file"
    assert json.loads(j[0]["function"]["arguments"]) == {"path": "a", "content": "b"}
    x = parse_text_calls("<tool_call>\n<function=run_shell>\n<parameter=command>\nls -la\n</parameter>\n"
                         "<parameter=timeout_s>\n30\n</parameter>\n</function>\n</tool_call>", NAMES)
    assert json.loads(x[0]["function"]["arguments"]) == {"command": "ls -la", "timeout_s": 30}
    f = parse_text_calls('Here:\n```json\n{"name": "read_file", "arguments": {"path": "x.py"}}\n```', NAMES)
    assert f[0]["function"]["name"] == "read_file"
    two = parse_text_calls('<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>'
                           '<tool_call>{"name":"read_file","arguments":{"path":"b"}}</tool_call>', NAMES)
    assert len(two) == 2
    assert parse_text_calls('<tool_call>{"name": "rm_everything", "arguments": {}}</tool_call>', NAMES) == []
    assert parse_text_calls("just a normal sentence with {braces}", NAMES) == []
    assert strip_calls('Creating.\n<tool_call>{"name":"x"}</tool_call>') == "Creating."


def test_normalize_prefers_real_calls_then_rescues():
    real = {"content": "ok", "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]}
    assert normalize(real, NAMES)[2] == real["tool_calls"] and normalize(real, NAMES)[3] is False
    text = {"content": 'x</think>\n<tool_call>{"name":"read_file","arguments":{"path":"a"}}</tool_call>'}
    content, reasoning, calls, rescued = normalize(text, NAMES)
    assert rescued and calls[0]["function"]["name"] == "read_file" and content == "" and reasoning == "x"


# ── agent jobs end-to-end (fake model server) ───────────────────────────
def agent_job(w, task, project, persona="general", translate=False, **extra):
    model = w.reg.personas[persona]["model"]
    return w.q.enqueue(model, {"task": task, "cwd": str(project), "depth": 0, "translate": translate,
                               "messages": [{"role": "user", "content": task}], **extra},
                       persona=persona, kind="agent")


def drain(w, limit=20):
    n = 0
    while (job := w.q.claim()) is not None and n < limit:
        w.handle(job)
        n += 1


@pytest.fixture
def ag(home):
    w, _ = make_rt()
    proj = get_paths().source / "proj"
    proj.mkdir(parents=True)
    (proj / "a.txt").write_text("original")
    yield w, proj
    w.stop_event.set()
    w.mm.stop()


def result_of(w, jid):
    return json.loads(w.q.get(jid)["result"])


def notes(w, jid):
    return [e["data"].get("note") or "" for e in w.q.events(jid) if e["type"] == "agent"]


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_agent_writes_file_after_snapshot_and_can_be_rolled_back(ag):
    w, proj = ag
    jid = agent_job(w, "please WRITE a greeting file", proj)
    drain(w)
    job = w.q.get(jid)
    assert job["status"] == "done", job["error"]
    res = result_of(w, jid)
    assert (proj / "hello.txt").read_text() == "hi there"
    assert res["message"]["content"].startswith("FINAL: created")
    assert res["agent"]["steps"] == 2 and res["agent"]["tools_used"] == 1
    assert "law rollback" in res["agent"]["undo"]
    assert store().run_count(proj, jid) >= 1                                        # raw reply saved (store)
    assert not (proj / ".local-agent").exists()                                     # nothing added to the project
    types = [e["type"] for e in w.q.events(jid)]
    assert "snapshot" in types and "agent" in types and "deltas" in types          # streamed live (thinking)
    Undo(proj).rollback(res["agent"]["snapshot"])
    assert not (proj / "hello.txt").exists() and (proj / "a.txt").read_text() == "original"


def test_thinking_only_reply_is_nudged_until_the_work_is_done(ag):
    """What happened on the M5 Pro: 'Simple task… </think>' and no tool call."""
    w, proj = ag
    jid = agent_job(w, "THINKLEAK create hello.py", proj)
    drain(w)
    res = result_of(w, jid)
    assert (proj / "hello.txt").exists()
    assert any("without using any tool" in n for n in notes(w, jid))
    assert "</think>" not in res["message"]["content"] and res["agent"]["tools_used"] == 1


@pytest.mark.parametrize("kw", ["TEXTCALL", "XMLCALL"])
def test_tool_calls_written_as_text_are_recovered(ag, kw):
    w, proj = ag
    jid = agent_job(w, f"{kw} create the file", proj)
    drain(w)
    assert (proj / "hello.txt").read_text().strip() == "hi there"
    assert any("recovered 1 tool call" in n for n in notes(w, jid))
    assert result_of(w, jid)["message"]["content"].startswith("FINAL: created")


def test_agent_that_never_acts_says_so_plainly(ag):
    w, proj = ag
    jid = agent_job(w, "LAZY do something", proj)
    drain(w)
    res = result_of(w, jid)
    assert res["agent"]["steps"] == 2 and res["agent"]["tools_used"] == 0          # a real answer: 1 nudge only
    assert "did not use any tool, so nothing was changed" in res["message"]["content"]


def test_agent_shell_blocked_and_bad_json_are_reported_to_the_model(ag):
    w, proj = ag
    j1 = agent_job(w, "SHELL please", proj)
    j2 = agent_job(w, "BLOCKED please", proj)
    j3 = agent_job(w, "BADJSON please", proj)
    drain(w)
    assert "shell-ok" in result_of(w, j1)["message"]["content"]
    assert "REFUSED" in result_of(w, j2)["message"]["content"]
    assert "not valid JSON" in result_of(w, j3)["message"]["content"]
    assert w.q.get(j2)["status"] == "done"


def test_agent_remember_feeds_future_prompts(ag):
    w, proj = ag
    agent_job(w, "REMEMBER this", proj)
    drain(w)
    assert "user likes tea" in store().memory_markdown(proj)
    assert "user likes tea" in prompts.system_prompt("general", proj, can_delegate=False, personas=[],
                                                     store=store())


def test_translator_spec_is_saved_and_used(ag):
    w, proj = ag
    jid = agent_job(w, "please WRITE a file", proj, translate=True)
    drain(w)
    assert w.q.get(jid)["status"] == "done"
    spec = store().get_spec(proj, jid)
    assert spec and "# Task: Test task" in spec and '"please WRITE a file"' in spec
    ev = [e for e in w.q.events(jid) if e["type"] == "spec"]
    assert ev and ev[0]["data"]["assumptions"] == ["port 8090 is free"]
    assert w.q.get_state(jid)["messages"][1]["content"].startswith("# Task: Test task")


def test_translator_retries_when_the_first_reply_has_no_json(ag):
    w, proj = ag
    jid = agent_job(w, "BADSPEC WRITE a file", proj, translate=True)
    drain(w)
    assert store().get_spec(proj, jid) is not None
    assert any(r["step"] == "translate-2" for r in store().get_runs(proj, jid))
    assert not any(e["type"] == "warning" for e in w.q.events(jid))


def test_delegation_runs_sub_job_then_resumes_parent(ag):
    w, proj = ag
    jid = agent_job(w, "DELEGATE the work", proj)
    drain(w)
    parent = w.q.get(jid)
    assert parent["status"] == "done", parent["error"]
    kids = w.q.children(jid)
    assert len(kids) == 1 and kids[0]["persona"] == "coder" and kids[0]["status"] == "done"
    assert kids[0]["model"] == "qwen3-coder-next" and kids[0]["priority"] == 1
    assert (proj / "hello.txt").exists()                               # the coder sub-job wrote it
    assert store().get_handoff(proj, kids[0]["id"]) is not None
    states = [e["data"].get("state") for e in w.q.events(jid) if e["type"] == "status"]
    assert "waiting" in states
    assert "Sub-job" in result_of(w, jid)["message"]["content"]       # parent got the child's report
    child_sys = w.q.get_state(kids[0]["id"])["messages"][0]["content"]
    assert "delegate tool" in w.q.get_state(jid)["messages"][0]["content"]
    assert ("delegate tool" in child_sys) == (1 < load().raw["runtime"]["agent"]["max_depth"])


def test_cancel_cascades_and_resume_continues(ag):
    w, proj = ag
    jid = agent_job(w, "DELEGATE the work", proj)
    w.handle(w.q.claim())                                              # parent delegates → waiting
    assert w.q.status(jid) == "waiting"
    child = w.q.children(jid)[0]["id"]
    assert w.q.cancel(jid)
    assert w.q.status(child) == "cancelled"
    assert w.q.revive(jid) and w.q.status(jid) == "queued"
    drain(w)
    assert w.q.status(jid) == "done"
    assert "SUB-TASK CANCELLED" in result_of(w, jid)["message"]["content"]
    assert w.q.status(child) == "cancelled"                            # not re-run


def test_child_failure_wakes_parent(ag):
    w, proj = ag
    jid = agent_job(w, "DELEGATE the work", proj)
    w.handle(w.q.claim())
    child = w.q.claim()
    w.q.finish(child["id"], "failed", error="boom")
    assert w.q.status(jid) == "queued"
    drain(w)
    assert "SUB-TASK FAILED" in result_of(w, jid)["message"]["content"]


def test_max_steps_auto_continues_then_stops_bounded(ag):
    # a tool-using agent that would run forever must NOT stop dead at the limit (that silently
    # loses a long build) nor loop forever: it auto-continues up to auto_resume times, then stops.
    w, proj = ag
    jid = agent_job(w, "GRIND forever", proj, max_steps=3, auto_resume=3)   # varying progress, never finishes
    drain(w)
    res = result_of(w, jid)
    assert res["agent"]["steps"] == 12                       # 3 * (3 auto-continues + 1), bounded
    assert "reached the step budget" in res["message"]["content"]
    notes = [(e["data"] or {}).get("note", "") for e in w.q.events(jid) if e["type"] == "agent"]
    assert any("auto-continuing" in n for n in notes)        # it extended the budget on its own


def test_repetition_loop_is_detected_and_stopped(ag):
    # a model that degenerates into repeating the SAME step must be caught, not loop to the ceiling
    # (this flooded Telegram with the same message — the 'verify the Sent folder' loop).
    w, proj = ag
    jid = agent_job(w, "LOOP forever", proj, max_steps=40, auto_resume=0)
    drain(w)
    res = result_of(w, jid)
    assert res["agent"]["steps"] < 40                        # stopped early, nowhere near the ceiling
    notes = [(e["data"] or {}).get("note", "") for e in w.q.events(jid) if e["type"] == "agent"]
    warns = [(e["data"] or {}).get("message", "") for e in w.q.events(jid) if e["type"] == "warning"]
    assert any("repeating the same step" in n for n in notes)            # nudged to stop first
    assert any("repetition loop persisted" in x for x in warns)          # then ended the job


def test_interrupted_agent_continues_from_saved_state(ag):
    w, proj = ag
    jid = agent_job(w, "please WRITE it", proj)
    job = w.q.claim()
    st = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "please WRITE it"},
                       {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "write_file", "arguments": "{}"}}]},
                       {"role": "tool", "tool_call_id": "c1", "content": "created X"}],
          "step": 1, "snapshot": None, "pending": {}, "project": str(proj)}   # older state: no tools_used
    w.q.save_state(jid, st)
    w.handle(job)
    res = result_of(w, jid)
    assert res["agent"]["steps"] == 2 and res["message"]["content"] == "FINAL: created X"
    assert not (proj / "hello.txt").exists()                           # step 1 was not repeated


def test_agent_refuses_home_and_root(ag):
    w, _ = ag
    jid = agent_job(w, "WRITE", get_paths().home)
    drain(w)
    job = w.q.get(jid)
    assert job["status"] == "failed" and job["attempts"] == 1 and "project folder" in job["error"]


# ── CLI ─────────────────────────────────────────────────────────────────
def test_cli_agent_detach_show_memory_and_legacy(home, capsys):
    proj = get_paths().source / "cliproj"
    assert cli.main(["agent", "-d", "-C", str(proj), "--no-translate", "make", "a", "README"]) == 0
    out = capsys.readouterr().out
    assert "agent job 1" in out and proj.is_dir() and not (proj / ".local-agent").exists()
    from law.runtime.db import Queue
    j = Queue(get_paths().queue_db).get(1)
    payload = json.loads(j["payload"])
    assert j["kind"] == "agent" and payload["task"] == "make a README" and payload["translate"] is False
    assert cli.main(["agent", "--show-memory", "-C", str(proj)]) == 0
    assert "AGENT.md" in capsys.readouterr().out
    assert cli.main(["agent", "-C", str(get_paths().home), "x"]) == 2
    assert cli.main(["--agent", "--show-memory", "-C", str(proj)]) == 0
    assert cli.main(["agent", "--resume", "999"]) == 1


def test_registry_context_and_agent_defaults():
    reg = load()
    assert reg.models["qwen38-27b"].context == 49152
    a = reg.raw["runtime"]["agent"]
    assert a["max_steps"] >= 10 and a["max_depth"] >= 1 and a["translate"] is True
    assert a["stream"] is True and a["nudges"] >= 1                 # turns stream (live thinking)
    assert a["context_chars"] <= 60000                             # bounded so the prompt stays readable in time
