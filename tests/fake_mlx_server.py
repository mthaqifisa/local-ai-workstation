"""Stand-in for mlx-openai-server in tests. Speaks just enough OpenAI API — both streamed
(SSE) and whole (stream: false) replies.

Plain chat keywords (last message): SLOW (tokens every 0.5s), THINK (reasoning delta),
TOOL (streams a tool call), CRASH (process exits mid-reply).
"secret code" prompts (law probe): answers the hidden LAW-###### code, unless the prompt is
longer than FAKE_RECALL_MAX_CHARS (then answers wrong, like a model past its limit).

Translator (system prompt mentions "requirements engineer") → a JSON task spec; with
BADSPEC in the request the first reply is prose (no JSON) and the retry gets the JSON.

Agent mode (request has `tools`; keyword taken from the FIRST user message):
  last message is a tool result         → final answer "FINAL: <first 80 chars of it>"
                                          (except LOOP: keeps calling write_file)
  a later user message (a law nudge)    → write_file hello.txt (except LAZY)
  BADJSON / BLOCKED / DELEGATE / REMEMBER / SHELL / LOOP / WRITE → that tool call
  THINKLEAK → "Simple task. …</think>" and NO tool call  (what qwen38-27b did for real)
  TEXTCALL  → the write_file call written as <tool_call>{json}</tool_call> TEXT
  XMLCALL   → the write_file call written as Qwen3-Coder <function=…> XML TEXT
  LAZY      → always a plain answer, never a tool
  no keyword → plain answer

Env: FAKE_DIE=1 → exit while "loading"; FAKE_LOAD_S → load delay;
     FAKE_USAGE=1 → report usage.prompt_tokens (= characters / 4);
     FAKE_SECRET_SLEEP=<s> → wait before answering a "secret code" prompt;
     FAKE_500_OVER_CHARS=<n> → prompts longer than n characters fail like the real 300 s
       limit: an error chunk inside the stream (FAKE_500_HTTP=1 or a whole reply: HTTP 500),
       and the log shows the mlx-openai-server TimeoutError traceback.
"""
import argparse
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--model-path", required=True)
args, _ = ap.parse_known_args()
if os.environ.get("FAKE_DIE"):
    print("boom: fake load failure (out of memory)", flush=True)
    sys.exit(3)
NAME = os.path.basename(args.model_path.rstrip("/"))
time.sleep(float(os.environ.get("FAKE_LOAD_S", "0.3")))
print(f"fake server {NAME} on {args.port}", flush=True)

SPEC = {"title": "Test task", "goal": "Do what the user asked", "assignee": "general",
        "deliverables": ["the result"], "constraints": [], "assumptions": ["port 8090 is free"],
        "steps": ["do it"], "acceptance_criteria": ["it works"], "verify_commands": ["true"],
        "blocking_questions": []}

AGENT_CALLS = [
    ("BADJSON", "write_file", "{not json"),
    ("BLOCKED", "run_shell", {"command": "rm -rf /"}),
    ("DELEGATE", "delegate", {"persona": "coder", "task": "WRITE child.txt please"}),
    ("REMEMBER", "remember", {"fact": "user likes tea"}),
    ("SHELL", "run_shell", {"command": "echo shell-ok"}),
    ("LOOP", "write_file", {"path": "loop.txt", "content": "x"}),
    ("WRITE", "write_file", {"path": "hello.txt", "content": "hi there"}),
]
WRITE_HELLO = {"path": "hello.txt", "content": "hi there"}
TEXT_JSON = ('Creating it now.\n<tool_call>\n{"name": "write_file", "arguments": '
             '{"path": "hello.txt", "content": "hi there"}}\n</tool_call>')
TEXT_XML = ("<tool_call>\n<function=write_file>\n<parameter=path>\nhello.txt\n</parameter>\n"
            "<parameter=content>\nhi there\n</parameter>\n</function>\n</tool_call>")


def call(name, a, say=""):
    ev = [({"content": say}, None)] if say else []
    ev.append(({"tool_calls": [{"index": 0, "id": f"call_{name}", "type": "function",
                                "function": {"name": name, "arguments": a if isinstance(a, str) else json.dumps(a)}}]},
               None))
    ev.append(({}, "tool_calls"))
    return ev


def text(t):
    return [({"content": t}, None), ({}, "stop")]


def plan(req):
    """→ (events, usage). events: (delta, finish) or ("SLEEP", s) / ("CRASH", None)."""
    msgs = req["messages"]
    last = msgs[-1]
    ltext = str(last.get("content"))
    system = str(msgs[0].get("content")) if msgs[0].get("role") == "system" else ""
    users = [m for m in msgs if m.get("role") == "user"]
    first = str(users[0].get("content")) if users else ltext

    if "requirements engineer" in system:
        if "BADSPEC" in ltext:
            return text("Sure, I will plan this task carefully."), None
        return text("Here is the spec:\n" + json.dumps(SPEC)), None

    if req.get("tools"):
        role = last.get("role")
        if "GRIND" in first:                              # makes VARYING progress forever (never finishes)
            n = sum(1 for m in msgs if m.get("role") == "tool")
            return call("run_shell", {"command": f"echo grind-{n}"}, f"grinding step {n}"), None
        if role == "tool" and "LOOP" not in first:
            return text("FINAL: " + str(last.get("content"))[:80]), None
        if role == "user" and len(users) > 1 and "LAZY" not in first:
            return call("write_file", WRITE_HELLO, "Doing it now."), None
        if "LAZY" in first:
            return text("I would create the file and run it."), None
        if "THINKLEAK" in first:
            return text("Simple task. Create the file, run it, verify.\n</think>\n\n"), None
        if "TEXTCALL" in first:
            return text(TEXT_JSON), None
        if "XMLCALL" in first:
            return text(TEXT_XML), None
        for kw, name, a in AGENT_CALLS:
            if kw in first:
                return call(name, a, f"Using {name}."), None

    if "secret code" in ltext:
        time.sleep(float(os.environ.get("FAKE_SECRET_SLEEP", "0")))
        m = re.search(r"LAW-\d+", ltext)
        limit = int(os.environ.get("FAKE_RECALL_MAX_CHARS", "10000000"))
        ans = m.group(0) if (m and len(ltext) <= limit) else "I don't know"
        usage = {"prompt_tokens": len(ltext) // 4, "completion_tokens": 3} if os.environ.get("FAKE_USAGE") else None
        return text(ans), usage

    ev = []
    if "THINK" in ltext:
        ev.append(({"reasoning_content": "hmm "}, None))
    if "TOOL" in ltext:
        ev += [({"tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                 "function": {"name": "get_time", "arguments": ""}}]}, None),
               ({"tool_calls": [{"index": 0, "function": {"arguments": '{"tz":'}}]}, None),
               ({"tool_calls": [{"index": 0, "function": {"arguments": '"UTC"}'}}]}, None),
               ({}, "tool_calls")]
        return ev, None
    for tok in ["Hello", " from", " ", NAME]:
        ev += [({"content": tok}, None), ("SLEEP", 0.01)]
    if "CRASH" in ltext:
        ev.append(("CRASH", None))
    if "SLOW" in ltext:
        for _ in range(60):
            ev += [({"content": "."}, None), ("SLEEP", 0.5)]
    ev.append(({}, "stop"))
    return ev, None


def whole(events):
    content, reasoning, tools, finish = [], [], {}, None
    for d, f in events:
        if d == "SLEEP":
            time.sleep(f)
            continue
        if d == "CRASH":
            os._exit(9)
        content.append(d.get("content") or "")
        reasoning.append(d.get("reasoning_content") or "")
        for tc in d.get("tool_calls") or []:
            cur = tools.setdefault(tc.get("index", 0), {"id": None, "type": "function",
                                                        "function": {"name": "", "arguments": ""}})
            cur["id"] = tc.get("id") or cur["id"]
            cur["function"]["name"] += (tc.get("function") or {}).get("name") or ""
            cur["function"]["arguments"] += (tc.get("function") or {}).get("arguments") or ""
        finish = f or finish
    msg = {"role": "assistant", "content": "".join(content)}
    if any(reasoning):
        msg["reasoning_content"] = "".join(reasoning)
    if tools:
        msg["tool_calls"] = [tools[i] for i in sorted(tools)]
    return msg, finish


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200, {"data": [{"id": NAME}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n))
        stream = bool(req.get("stream"))
        ltext = str(req["messages"][-1].get("content"))
        over = int(os.environ.get("FAKE_500_OVER_CHARS", "0") or 0)
        if over and len(ltext) > over:
            print(f"BatchScheduler admitted uid=1 (active=1, prompt_tokens={len(ltext) // 4}, cached_prefix=0)",
                  flush=True)
            print("    response = await asyncio.wait_for(result_queue.get(), timeout=self._rpc_timeout)\n"
                  "TimeoutError", flush=True)
            err = {"message": "", "type": "server_error", "param": None, "code": "500"}
            if os.environ.get("FAKE_500_HTTP") or not stream:
                return self._json(500, {"error": err})
            self.send_response(200)                       # what 1.8.1 really does: an error inside the stream
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(("data: " + json.dumps({"error": err}) + "\n\n").encode())
            self.wfile.flush()
            return

        events, usage = plan(req)
        if not stream:
            msg, finish = whole(events)
            out = {"id": "fake", "object": "chat.completion",
                   "choices": [{"index": 0, "message": msg, "finish_reason": finish or "stop"}]}
            if usage:
                out["usage"] = usage
            return self._json(200, out)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for d, f in events:
            if d == "SLEEP":
                time.sleep(f)
                continue
            if d == "CRASH":
                os._exit(9)
            self.wfile.write(("data: " + json.dumps({"choices": [{"index": 0, "delta": d,
                                                                    "finish_reason": f}]}) + "\n\n").encode())
            self.wfile.flush()
        if usage:
            self.wfile.write(("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
