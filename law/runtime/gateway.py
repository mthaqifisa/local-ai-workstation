"""OpenAI- and Anthropic-compatible gateway on <bind_host>:<gateway port> (127.0.0.1 by default;
LAW_BIND_HOST=0.0.0.0 opens it to your LAN). No auth — every request passes law.runtime.netguard
(trusted networks only, Host + Origin checks) so other machines and web pages can't use it.

Every request becomes a queue job, so Open WebUI, OpenCode, Claude Code, Telegram, the CLI
and the dashboard all take turns — never two models or two jobs at once.

  GET  /health                        worker + queue status
  GET  /v1/models                     personas + downloaded chat models
  POST /v1/chat/completions           OpenAI chat (stream or not; tools pass through)
  POST /v1/messages                   Anthropic Messages API (stream or not; tools)
  POST /v1/messages/count_tokens      Anthropic token estimate
  GET  /law/jobs                      recent jobs
  GET  /law/jobs/<id>                 one job
  GET  /law/jobs/<id>/events?after=N
  POST /law/jobs/<id>/cancel

OpenAI streams: while a job waits in line or a model loads, PROGRESS lines appear in the
"thinking" area (reasoning_content) — e.g. "[law] Loading qwen38-27b (~16 GB)…". They are
never part of the answer and stop as soon as the model answers.
Turn off with runtime.gateway.progress: false.
Anthropic streams: Anthropic `ping` events keep the connection alive instead.
If a streaming client disconnects, its job is cancelled.
"""
from __future__ import annotations

import json
import logging
import re
import select
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from urllib.parse import parse_qs, urlparse

from ..doctor import cached
from ..paths import get_paths
from ..registry import load
from . import anthropic as anth
from .db import TERMINAL, Queue
from .netguard import Guard

log = logging.getLogger("law.gateway")
CHAT_KINDS = ("lm", "vlm")
PASS_PARAMS = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "tools", "tool_choice",
               "response_format", "seed", "presence_penalty", "frequency_penalty", "chat_template_kwargs")


def progress_line(t: str, d: dict, position: int = 0) -> str | None:
    """Human-readable progress for a queue event, or None if not worth showing."""
    if t == "status":
        if d.get("state") == "queued":
            why = f" ({d['reason']})" if d.get("reason") else ""
            return f"[law] Waiting in queue{f', position {position}' if position else ''}{why}…"
        if d.get("state") == "running" and (d.get("attempt") or 1) > 1:
            return f"[law] Retrying (attempt {d['attempt']})…"
        return None
    if t == "model":
        st, m = d.get("state"), d.get("model")
        return {"unloading": f"[law] Unloading {m} to free memory…",
                "waiting_ram": f"[law] Waiting for memory: {d.get('free_gb')} GB free, {m} needs ~{d.get('need_gb')} GB…",
                "loading": f"[law] Loading {m} (~{d.get('gb')} GB) — the first answer takes a moment…",
                "ready": f"[law] {m} ready in {d.get('load_s')}s."}.get(st)
    if t == "warning":
        return f"[law] Warning: {d.get('message')}"
    return None


def follow(q: Queue, jid: int, *, idle_note_s: float = 20, keepalive_s: float = 5,
           poll_s: float = 0.15) -> Iterator[tuple[str, object]]:
    """Follow a job's events. Yields:
       ("delta", dict)  model output          ("progress", str)  human progress (before the answer)
       ("keepalive", str) nothing new          ("done", result) / ("error", msg) / ("cancelled", reason)"""
    after, answering = 0, False
    last_out = last_status = last_note = start = time.time()
    while True:
        evs = q.events(jid, after)
        for e in evs:
            after = e["seq"]
            t, d = e["type"], e["data"] or {}
            if t == "deltas":
                answering = True
                for delta in d:
                    yield "delta", delta
            elif t == "done":
                yield "done", d
                return
            elif t == "error":
                yield "error", d.get("message") or "job failed"
                return
            elif t == "cancelled":
                yield "cancelled", d.get("reason") or "cancelled"
                return
            else:
                line = progress_line(t, d, q.position(jid))
                if line and not answering:
                    last_note = time.time()
                    yield "progress", line
                else:
                    yield "keepalive", f"{t} {json.dumps(d)}"
        now = time.time()
        if evs:
            last_out = now
        elif not answering and now - last_note > idle_note_s:
            pos = q.position(jid)
            last_note = last_out = now
            yield "progress", (f"[law] Still waiting in queue (position {pos}, {now - start:.0f}s)…" if pos
                               else f"[law] Still working… ({now - start:.0f}s)")
        elif now - last_out > keepalive_s:
            last_out = now
            pos = q.position(jid)
            yield "keepalive", f"waiting (queue position {pos})" if pos else "working"
        if now - last_status > 2:              # safety net: terminal without an event
            last_status = now
            st = q.status(jid)
            if st in TERMINAL and not q.events(jid, after):
                job = q.get(jid) or {}
                if st == "done":
                    yield "done", json.loads(job.get("result") or "{}")
                elif st == "failed":
                    yield "error", job.get("error") or "job failed"
                else:
                    yield "cancelled", job.get("error") or "cancelled"
                return
        time.sleep(poll_s)


class Gateway:
    def __init__(self, paths, reg, q: Queue):
        self.paths, self.reg, self.q = paths, reg, q
        gcfg = (reg.raw.get("runtime") or {}).get("gateway") or {}
        self.progress = bool(gcfg.get("progress", True))
        self.anthropic_rules = dict(gcfg.get("anthropic_models") or {"haiku": "general", "*": "coder"})
        self.guard = Guard()

    def resolve(self, name: str | None) -> tuple[str, str | None] | None:
        """model field → (model id, persona). Accepts persona names and model ids."""
        name = (name or "").strip()
        if name in ("", "law", "default"):
            name = self.reg.raw.get("defaults", {}).get("persona", "general")
        if name in self.reg.personas:
            return self.reg.personas[name]["model"], name
        if name in self.reg.models and self.reg.models[name].kind in CHAT_KINDS:
            return name, None
        return None

    def resolve_anthropic(self, name: str) -> tuple[str, str | None] | None:
        default = self.reg.raw.get("defaults", {}).get("persona", "general")
        return self.resolve(anth.resolve_model(name, self.reg.personas, self.reg.models,
                                               self.anthropic_rules, default))

    def model_list(self) -> list[dict]:
        out, now = [], int(time.time())
        for name, p in self.reg.personas.items():
            if cached(self.paths, self.reg.models[p["model"]].repo):
                out.append({"id": name, "object": "model", "created": now, "owned_by": "law-persona"})
        for m in self.reg.models.values():
            if m.kind in CHAT_KINDS and cached(self.paths, m.repo):
                out.append({"id": m.id, "object": "model", "created": now, "owned_by": "law"})
        return out

    def serve(self, host: str, port: int) -> ThreadingHTTPServer:
        gw = self

        class H(Handler):
            g = gw
        srv = ThreadingHTTPServer((host, port), H)
        srv.daemon_threads = True
        return srv


class Handler(BaseHTTPRequestHandler):
    g: Gateway
    protocol_version = "HTTP/1.1"
    server_version = "law-gateway"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # ── helpers ─────────────────────────────────────────────────────────
    def _cors(self):
        # only an Origin that passed the guard is echoed — never "*" (any web page could read chats)
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _refused(self) -> bool:
        why = self.g.guard.check(self.client_address[0], self.headers, write=self.command == "POST")
        if why:
            log.warning("refused %s %s from %s: %s", self.command, self.path[:80], self.client_address[0], why)
            body = json.dumps({"error": {"message": why, "type": "forbidden"}}).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return bool(why)

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str, typ: str = "invalid_request_error") -> None:
        self._json(code, {"error": {"message": msg, "type": typ}})

    def _aerr(self, code: int, msg: str, typ: str = "invalid_request_error") -> None:
        self._json(code, {"type": "error", "error": {"type": typ, "message": msg}})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _sse_headers(self, jid: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Law-Job", str(jid))
        self._cors()
        self.end_headers()
        self.close_connection = True

    def _client_gone(self) -> bool:
        """True if the client closed the connection (readable + 0 bytes peeked)."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _wait(self, jid: int) -> tuple[str, dict] | None:
        """Wait for a non-streamed job. None = the client hung up (the job is cancelled, so it
        doesn't keep the single worker busy for nobody)."""
        q = self.g.q
        last = time.time()
        while (st := q.status(jid)) not in TERMINAL:
            time.sleep(0.25)
            if time.time() - last >= 1:
                last = time.time()
                if self._client_gone():
                    if q.cancel(jid, reason="client disconnected"):
                        log.info("job %s cancelled: client disconnected", jid)
                    self.close_connection = True
                    return None
        return st, q.get(jid)

    # ── routes ──────────────────────────────────────────────────────────
    def do_OPTIONS(self):
        if self._refused():
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self._refused():
            return
        u = urlparse(self.path)
        q = self.g.q
        if u.path == "/health":
            return self._json(200, {"ok": True, "worker": q.worker_info(), "queued": q.depth()})
        if u.path == "/v1/models":
            return self._json(200, {"object": "list", "data": self.g.model_list()})
        if u.path == "/law/jobs":
            return self._json(200, {"jobs": q.jobs(50)})
        m = re.fullmatch(r"/law/jobs/(\d+)(/events)?", u.path)
        if m:
            jid = int(m.group(1))
            if m.group(2):
                after = int(parse_qs(u.query).get("after", ["0"])[0])
                return self._json(200, {"events": q.events(jid, after)})
            job = q.get(jid)
            return self._json(200, job) if job else self._err(404, f"job {jid} not found")
        return self._err(404, f"no route {u.path}")

    def do_POST(self):
        if self._refused():
            return
        u = urlparse(self.path)
        m = re.fullmatch(r"/law/jobs/(\d+)/cancel", u.path)
        if m:
            ok = self.g.q.cancel(int(m.group(1)), reason="cancelled via API")
            return self._json(200, {"cancelled": ok})
        if u.path in ("/v1/chat/completions", "/chat/completions"):
            return self._chat()
        if u.path in ("/v1/messages", "/messages"):
            return self._messages()
        if u.path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            try:
                return self._json(200, {"input_tokens": anth.count_tokens(self._body())})
            except json.JSONDecodeError:
                return self._aerr(400, "body is not valid JSON")
        return self._err(404, f"no route {u.path}")

    # ── OpenAI chat ─────────────────────────────────────────────────────
    def _chat(self):
        try:
            req = self._body()
        except json.JSONDecodeError:
            return self._err(400, "body is not valid JSON")
        msgs = req.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return self._err(400, "'messages' must be a non-empty list")
        name = req.get("model") or "default"
        target = self.g.resolve(name)
        if target is None:
            return self._err(404, f"unknown model '{name}'. GET /v1/models lists what's available",
                             "model_not_found")
        model_id, persona = target
        params = {k: req[k] for k in PASS_PARAMS if k in req}
        jid = self.g.q.enqueue(model_id, {"messages": msgs, "params": params}, persona=persona, source="api")
        if req.get("stream"):
            return self._chat_stream(jid, name)
        waited = self._wait(jid)
        if waited is None:
            return None
        st, job = waited
        if st == "done":
            res = json.loads(job["result"])
            return self._json(200, {
                "id": f"chatcmpl-law-{jid}", "object": "chat.completion", "created": int(job["created"]),
                "model": name, "choices": [{"index": 0, "message": res["message"],
                                            "finish_reason": res["finish_reason"]}],
                "usage": res.get("usage") or {}})
        return self._err(500 if st == "failed" else 409, f"job {jid} {st}: {job.get('error') or ''}",
                         "job_" + st)

    def _chat_stream(self, jid: int, name: str):
        q = self.g.q
        self._sse_headers(jid)
        cid, created = f"chatcmpl-law-{jid}", int(time.time())

        def chunk(delta: dict, finish=None) -> None:
            obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": name,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")

        def comment(text: str) -> None:
            self.wfile.write(f": {text}\n\n".encode())

        try:
            chunk({"role": "assistant", "content": ""})
            self.wfile.flush()
            for kind, val in follow(q, jid):
                if kind == "delta":
                    chunk(val)
                elif kind == "progress":
                    if self.g.progress:
                        chunk({"reasoning_content": val + "\n"})
                    else:
                        comment(val)
                elif kind == "keepalive":
                    comment(val)
                elif kind == "done":
                    chunk({}, (val or {}).get("finish_reason") or "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                elif kind == "error":
                    self.wfile.write(b"data: " + json.dumps({"error": {"message": val}}).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                elif kind == "cancelled":
                    chunk({}, "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if q.cancel(jid, reason="client disconnected"):
                log.info("job %s cancelled: client disconnected", jid)

    # ── Anthropic messages ──────────────────────────────────────────────
    def _messages(self):
        try:
            req = self._body()
        except json.JSONDecodeError:
            return self._aerr(400, "body is not valid JSON")
        try:
            msgs, params = anth.to_openai(req)
        except anth.AnthropicError as e:
            return self._aerr(400, str(e))
        name = req.get("model") or ""
        target = self.g.resolve_anthropic(name)
        if target is None:
            return self._aerr(404, f"no local model for '{name}' — check runtime.gateway.anthropic_models",
                              "not_found_error")
        model_id, persona = target
        jid = self.g.q.enqueue(model_id, {"messages": msgs, "params": params}, persona=persona,
                               source="anthropic")
        msg_id = f"msg_law_{jid}"
        if req.get("stream"):
            return self._messages_stream(jid, name, msg_id)
        waited = self._wait(jid)
        if waited is None:
            return None
        st, job = waited
        if st == "done":
            return self._json(200, anth.from_openai(json.loads(job["result"]), name, msg_id))
        return self._aerr(500 if st == "failed" else 409, f"job {jid} {st}: {job.get('error') or ''}",
                          "api_error")

    def _messages_stream(self, jid: int, name: str, msg_id: str):
        q = self.g.q
        self._sse_headers(jid)
        tr = anth.StreamTranslator(name, msg_id)

        def emit(events) -> None:
            for ev, data in events:
                self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())

        try:
            emit(tr.start())
            self.wfile.flush()
            for kind, val in follow(q, jid):
                if kind == "delta":
                    emit(tr.delta(val))
                elif kind in ("progress", "keepalive"):
                    emit([("ping", {"type": "ping"})])
                elif kind == "done":
                    emit(tr.finish((val or {}).get("finish_reason"), (val or {}).get("usage")))
                elif kind == "error":
                    emit([("error", {"type": "error", "error": {"type": "api_error", "message": val}})])
                elif kind == "cancelled":
                    emit(tr.finish("stop"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if q.cancel(jid, reason="client disconnected"):
                log.info("job %s cancelled: client disconnected", jid)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s gateway %(levelname)s %(message)s")
    paths = get_paths()
    reg = load(paths)
    gw = Gateway(paths, reg, Queue(paths.queue_db))
    srv = gw.serve(reg.bind_host, reg.ports["gateway"])
    log.info("gateway listening on %s:%s", reg.bind_host, reg.ports["gateway"])
    srv.serve_forever()


if __name__ == "__main__":
    main()
