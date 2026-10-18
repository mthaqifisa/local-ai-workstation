"""The single worker: takes ONE job at a time from the queue and runs it.

Runs as launchd service com.law.worker (KeepAlive). On start it puts back any job a
crash left 'running' (or fails it if it keeps getting interrupted) and kills any orphaned
model process. Between jobs it unloads the model after `idle_unload_s`.

A job never hangs forever:
  - model load has a timeout and is watched for crashes
  - streaming has a first-token timeout and a between-tokens (idle) timeout
  - cancellation is checked twice a second
  - failures are retried (max_attempts), then reported as 'failed' with the reason
  - a heartbeat thread reports "alive" every few seconds and the job loop reports
    "progress"; the watchdog (com.law.watchdog) restarts the worker if either stops

Job kinds: chat (normal requests), probe (`law probe`, see probe.py) and agent
(`law agent`, see law/agent/runner.py). An agent that hands work to sub-jobs is parked as
'waiting' and the worker moves on to the sub-jobs; the agent continues when they end.

Two ways to call the model:
  complete()       streamed — chats (tokens appear as they come), probes
  complete_once()  one whole reply — agent turns, the request translator, and chat requests
                   that carry tools (runtime.gateway.tools_whole_reply), because the model
                   server parses tool calls more reliably for complete replies
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import traceback
import urllib.error
import urllib.request

from ..paths import get_paths
from ..registry import load
from . import memory
from .db import DEFAULT_MAX_INTERRUPTIONS, Queue
from .modelproc import (MlxOpenAIServerLauncher, ModelError, ModelManager, ModelRefused, PromptTooSlow,
                        explain_server_error, kill_from_pidfile, log_size)

log = logging.getLogger("law.worker")

DEFAULTS = {"max_attempts": 2, "max_interruptions": DEFAULT_MAX_INTERRUPTIONS, "idle_unload_s": 900,
            "load_timeout_s": 1800, "first_token_timeout_s": 1800, "idle_token_timeout_s": 600,
            "default_max_tokens": 16384, "ram_wait_s": 120, "heartbeat_s": 5,
            "pressure_check_s": 15}


class Cancelled(Exception):
    pass


class Interrupted(Exception):
    pass


class Assembler:
    """Builds the final assistant message from streamed deltas (content, reasoning, tool calls)."""

    def __init__(self):
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict] = {}
        self.finish: str | None = None
        self.usage: dict | None = None

    def add(self, d: dict, finish: str | None = None) -> None:
        if d.get("content"):
            self.content.append(d["content"])
        r = d.get("reasoning_content") or d.get("reasoning")
        if r:
            self.reasoning.append(r)
        for tc in d.get("tool_calls") or []:
            i = tc.get("index", len(self.tools))
            cur = self.tools.setdefault(i, {"id": None, "type": "function",
                                            "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                cur["id"] = tc["id"]
            f = tc.get("function") or {}
            cur["function"]["name"] += f.get("name") or ""
            args = f.get("arguments")
            cur["function"]["arguments"] += (args if isinstance(args, str) else json.dumps(args)) if args else ""
        if finish:
            self.finish = finish

    def result(self) -> dict:
        msg: dict = {"role": "assistant", "content": "".join(self.content)}
        if self.reasoning:
            msg["reasoning_content"] = "".join(self.reasoning)
        if self.tools:
            msg["tool_calls"] = [self.tools[i] for i in sorted(self.tools)]
        return {"message": msg, "finish_reason": self.finish or ("tool_calls" if self.tools else "stop"),
                "usage": self.usage}


def _set_read_timeout(resp, seconds: float) -> None:
    try:
        resp.fp.raw._sock.settimeout(seconds)
    except Exception:  # noqa: BLE001 — best effort; the first-token timeout still applies
        pass


class Worker:
    def __init__(self, paths, reg, q: Queue, mm: ModelManager, cfg: dict | None = None,
                 pressure=None):
        self.paths, self.reg, self.q, self.mm = paths, reg, q, mm
        self.cfg = {**DEFAULTS, **((reg.raw.get("runtime") or {}).get("worker") or {}), **(cfg or {})}
        self.stop_event = threading.Event()
        self.pid = os.getpid()
        self.pressure = pressure or memory.pressure
        self.state = {"job_id": None, "state": "idle"}
        self.progress = time.time()
        self.mm.tick = self._touch

    # ── heartbeat ───────────────────────────────────────────────────────
    def _touch(self) -> None:
        self.progress = time.time()

    def _beat(self) -> None:
        self.q.beat(self.pid, self.state["job_id"], self.mm.current, self.state["state"], self.progress)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.cfg["heartbeat_s"]):
            try:
                self._beat()
            except Exception as e:  # noqa: BLE001 — never let the heartbeat die
                log.warning("heartbeat failed: %s", e)

    # ── loop ────────────────────────────────────────────────────────────
    def startup(self) -> None:
        if kill_from_pidfile(self.mm.pidfile):
            log.warning("killed an orphaned model process from a previous run")
        back = self.q.requeue_running(self.cfg["max_interruptions"])
        if back:
            log.warning("put back interrupted jobs: %s", back)

    def run_forever(self) -> None:
        self.startup()
        hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()
        idle_since = time.time()
        log.info("worker ready (pid %s)", self.pid)
        while not self.stop_event.is_set():
            self.state.update(job_id=None, state="idle")
            self._touch()
            self._beat()
            job = self.q.claim()
            if job is None:
                if self.mm.current and time.time() - idle_since > self.cfg["idle_unload_s"]:
                    log.info("idle for %ss → unloading %s", self.cfg["idle_unload_s"], self.mm.current)
                    self.mm.stop()
                self.stop_event.wait(0.5)
                continue
            self.handle(job)
            idle_since = time.time()
        self.mm.stop()
        self.state.update(job_id=None, state="stopped")
        self._beat()
        log.info("worker stopped")

    # ── one job ─────────────────────────────────────────────────────────
    def handle(self, job: dict) -> None:
        from ..agent.runner import ContinueJob, WaitingForChildren
        jid = job["id"]
        self.mm.emit = lambda t, d: self.q.add_event(jid, t, d)
        self.state.update(job_id=jid, state="running")
        self._touch()
        self._beat()
        self.q.add_event(jid, "status", {"state": "running", "attempt": job["attempts"]})
        log.info("job %s start (%s, model %s, attempt %s)", jid, job["kind"], job["model"], job["attempts"])
        try:
            if job["kind"] == "chat":
                result = self._chat(job)
            elif job["kind"] == "probe":
                from .probe import run_probe
                result = run_probe(self, job)
            elif job["kind"] == "agent":
                from ..agent.runner import run_agent
                result = run_agent(self, job)
            elif job["kind"] == "image":
                result = self._image(job)
            elif job["kind"] == "tts":
                result = self._tts(job)
            elif job["kind"] == "video":
                result = self._video(job)
            elif job["kind"] == "index":
                result = self._index(job)
            elif job["kind"] == "search":
                result = self._search(job)
            elif job["kind"] == "vision":
                result = self._vision(job)
            elif job["kind"] == "stt":
                result = self._stt(job)
            else:
                raise ModelRefused(f"unknown job kind '{job['kind']}'")
            self.q.finish(jid, "done", result=result)
            log.info("job %s done", jid)
        except WaitingForChildren as wc:
            self.q.wait_for_children(jid, wc.children)
            log.info("job %s waits for sub-jobs %s", jid, wc.children)
        except ContinueJob:
            self.q.requeue(jid, reason="auto-continue: extended step budget")
            log.info("job %s auto-continuing (step budget extended)", jid)
        except Cancelled:
            log.info("job %s cancelled", jid)
        except Interrupted:
            self.q.requeue(jid, reason="worker stopping", undo_attempt=True,
                           max_interruptions=self.cfg["max_interruptions"])
            log.info("job %s put back (worker stopping)", jid)
        except Exception as e:  # noqa: BLE001
            msg = str(e) or type(e).__name__
            log.error("job %s error: %s\n%s", jid, msg, traceback.format_exc())
            if self.mm.current and isinstance(e, ModelError) and not isinstance(e, (ModelRefused, PromptTooSlow)):
                self.mm.stop()               # retry on a freshly started model, never a half-dead one
            retry = getattr(e, "retry", True) and job["attempts"] < self.cfg["max_attempts"]
            if retry and self.q.requeue(jid, reason=f"retrying after error: {msg[:300]}"):
                pass
            else:
                self.q.finish(jid, "failed", error=msg)
        finally:
            self.mm.emit = lambda t, d: None
            # a cowork job takes many screenshots (screenshot→look_at→act→screenshot); once the job is
            # truly finished (not just paused/requeued), delete them so they don't pile up.
            if job.get("kind") == "agent" and self.q.status(jid) in ("done", "failed", "cancelled"):
                try:
                    import shutil
                    proj = json.loads(job["payload"]).get("cwd")
                    if proj:
                        shutil.rmtree(os.path.join(os.path.expanduser(str(proj)), ".screenshots"),
                                      ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass

    def check(self, jid: int) -> None:
        if self.stop_event.is_set():
            raise Interrupted()
        if self.q.status(jid) == "cancelled":
            raise Cancelled()

    _check = check

    def _image(self, job: dict) -> dict:
        """Generate an image (FLUX via mflux). Frees the language model first so the image model
        gets the RAM/GPU, then runs mflux and saves the PNG."""
        from . import imagegen
        jid = job["id"]
        payload = json.loads(job["payload"])
        if not imagegen.available(self.paths):
            raise ModelRefused("image generation is not installed (mflux missing in the model-server venv)")
        self.check(jid)
        if self.mm.current:
            self.mm.stop()                        # one GPU user at a time
        self.check(jid)
        cfg = payload.get("params") or {}
        self.q.add_event(jid, "image", {"state": "generating", "model": f"flux-{cfg.get('base_model', 'schnell')}",
                                        "prompt": (payload.get("prompt") or "")[:200]})
        last = [0.0]

        def on_log(_line: str) -> None:
            self._touch()                         # keep the heartbeat alive during the long run
            now = time.time()
            if now - last[0] >= 5:
                last[0] = now
                self.q.add_event(jid, "image", {"state": "progress", "note": _line[:160]})
        res = imagegen.generate(self.paths, payload.get("prompt", ""), cfg=cfg, seed=payload.get("seed"),
                                touch=self._touch, check=lambda: self.check(jid), on_log=on_log)
        self.q.add_event(jid, "image", {"state": "done", **res})
        return {"image": res, "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"Generated image ({res['model']}, "
                            f"{res['seconds']}s): {res['path']}"}}

    def _tts(self, job: dict) -> dict:
        """Text-to-speech (Kokoro via mlx-audio). Frees the language model first."""
        from . import ttsgen
        jid = job["id"]
        payload = json.loads(job["payload"])
        if not ttsgen.available(self.paths):
            raise ModelRefused("text-to-speech is not installed (mlx-audio missing in the model-server venv)")
        self.check(jid)
        if self.mm.current:
            self.mm.stop()
        self.check(jid)
        cfg = payload.get("params") or {}
        self.q.add_event(jid, "tts", {"state": "generating", "voice": cfg.get("voice", "af_sky")})
        last = [0.0]

        def on_log(_line: str) -> None:
            self._touch()
            now = time.time()
            if now - last[0] >= 5:
                last[0] = now
                self.q.add_event(jid, "tts", {"state": "progress", "note": _line[:160]})
        res = ttsgen.generate(self.paths, payload.get("text", ""), cfg=cfg,
                              touch=self._touch, check=lambda: self.check(jid), on_log=on_log)
        self.q.add_event(jid, "tts", {"state": "done", **res})
        return {"tts": res, "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"Generated speech ({res['voice']}, "
                            f"{res['seconds']}s): {res['path']}"}}

    def _video(self, job: dict) -> dict:
        """Text/image-to-video (LTX-2.3 via mlx-video). Frees the language model first."""
        from . import videogen
        jid = job["id"]
        payload = json.loads(job["payload"])
        if not videogen.available(self.paths):
            raise ModelRefused("video generation is not installed (mlx-video missing in the model-server venv)")
        self.check(jid)
        if self.mm.current:
            self.mm.stop()
        self.check(jid)
        cfg = payload.get("params") or {}
        self.q.add_event(jid, "video", {"state": "generating",
                                        "prompt": (payload.get("prompt") or "")[:200]})
        last = [0.0]

        def on_log(_line: str) -> None:
            self._touch()
            now = time.time()
            if now - last[0] >= 8:
                last[0] = now
                self.q.add_event(jid, "video", {"state": "progress", "note": _line[:160]})
        res = videogen.generate(self.paths, payload.get("prompt", ""), cfg=cfg, image=payload.get("image"),
                               touch=self._touch, check=lambda: self.check(jid), on_log=on_log)
        self.q.add_event(jid, "video", {"state": "done", **res})
        return {"video": res, "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"Generated video ({res['seconds']}s): {res['path']}"}}

    def _index(self, job: dict) -> dict:
        """Embed every text file under a folder (RAG). Loads the embedding model."""
        from . import rag
        jid = job["id"]
        payload = json.loads(job["payload"])
        self.check(jid)
        base = self.mm.ensure(rag.EMBED_MODEL)
        self.check(jid)
        self.q.add_event(jid, "index", {"state": "indexing", "root": payload.get("root")})

        def on_prog(done, total, path):
            self._touch()
            self.q.add_event(jid, "index", {"state": "progress", "done": done, "total": total, "path": path})
        res = rag.index(self.paths, base, self.mm.served_name, payload.get("root", "."),
                        touch=self._touch, check=lambda: self.check(jid), on_progress=on_prog)
        self.q.add_event(jid, "index", {"state": "done", **res})
        return {"index": res, "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"Indexed {res['files']} files "
                            f"({res['chunks']} chunks) from {res['root']}"}}

    def _search(self, job: dict) -> dict:
        """Semantic search over an indexed folder (RAG). Loads the embedding model."""
        from . import rag
        jid = job["id"]
        payload = json.loads(job["payload"])
        self.check(jid)
        base = self.mm.ensure(rag.EMBED_MODEL)
        res = rag.search(self.paths, base, self.mm.served_name, payload.get("root", "."),
                         payload.get("query", ""), k=int(payload.get("k", 8)))
        return {"results": res, "finish_reason": "stop",
                "message": {"role": "assistant", "content": f"{len(res)} results"}}

    def _vision(self, job: dict) -> dict:
        """Describe / analyse an image with qwen3-vl-8b. Frees the language model first (the VLM needs
        the GPU), then runs mlx-vlm's CLI (the server's own VLM path is broken on 1.8.1)."""
        from . import visiongen
        jid = job["id"]
        payload = json.loads(job["payload"])
        image = payload.get("image")
        if not image or not os.path.exists(os.path.expanduser(str(image))):
            raise ModelRefused(f"vision job has no readable image (got {image!r})")
        if not visiongen.available(self.paths):
            raise ModelRefused("vision is not installed (mlx-vlm missing in the model-server venv)")
        self.check(jid)
        if self.mm.current:
            self.mm.stop()                        # one GPU user at a time
        self.check(jid)
        self.q.add_event(jid, "vision", {"state": "analysing", "image": os.path.basename(str(image))})
        last = [0.0]

        def on_log(_line: str) -> None:
            self._touch()
            now = time.time()
            if now - last[0] >= 5:
                last[0] = now
                self.q.add_event(jid, "vision", {"state": "progress", "note": _line[:160]})
        res = visiongen.describe(self.paths, image, payload.get("prompt") or "",
                                 cfg=payload.get("params") or {},
                                 touch=self._touch, check=lambda: self.check(jid), on_log=on_log)
        self.q.add_event(jid, "vision", {"state": "done", "seconds": res["seconds"]})
        return {"vision": res, "finish_reason": "stop",
                "message": {"role": "assistant", "content": res["text"]}}

    def _stt(self, job: dict) -> dict:
        """Transcribe speech to text (whisper). Frees the language model first (GPU)."""
        from . import sttgen
        jid = job["id"]
        payload = json.loads(job["payload"])
        audio = payload.get("audio")
        if not audio or not os.path.exists(os.path.expanduser(str(audio))):
            raise ModelRefused(f"stt job has no readable audio (got {audio!r})")
        if not sttgen.available(self.paths):
            raise ModelRefused("speech-to-text is not installed (mlx-whisper missing in the model-server venv)")
        self.check(jid)
        if self.mm.current:
            self.mm.stop()
        self.check(jid)
        self.q.add_event(jid, "stt", {"state": "transcribing"})
        res = sttgen.transcribe(self.paths, audio, cfg=payload.get("params") or {},
                                touch=self._touch, check=lambda: self.check(jid))
        self.q.add_event(jid, "stt", {"state": "done", "seconds": res["seconds"]})
        return {"stt": res, "finish_reason": "stop",
                "message": {"role": "assistant", "content": res["text"]}}

    def _chat(self, job: dict) -> dict:
        jid = job["id"]
        payload = json.loads(job["payload"])
        self.check(jid)
        base = self.mm.ensure(job["model"])
        self.check(jid)
        params = payload.get("params") or {}
        gcfg = (self.reg.raw.get("runtime") or {}).get("gateway") or {}
        if params.get("tools") and gcfg.get("tools_whole_reply", True):
            return self._chat_with_tools(jid, base, payload["messages"], params)
        return self.complete(jid, base, payload["messages"], params)["result"]

    def _chat_with_tools(self, jid: int, base: str, messages: list, params: dict) -> dict:
        """Chat request that carries tools (OpenCode, Claude Code, …): fetch the whole reply,
        recover tool calls the model wrote as text, then publish it as stream deltas so the
        gateway can still stream it to the client."""
        from ..agent.runner import normalize
        res = self.complete_once(jid, base, messages, params)["result"]
        names = {(t.get("function") or {}).get("name") for t in params.get("tools") or []} - {None}
        content, reasoning, calls, rescued = normalize(res["message"], names)
        msg: dict = {"role": "assistant", "content": content}
        if reasoning:
            msg["reasoning_content"] = reasoning
        deltas: list[dict] = []
        if reasoning:
            deltas.append({"reasoning_content": reasoning})
        if content:
            deltas.append({"content": content})
        if calls:
            out = []
            for i, c in enumerate(calls):
                fn = c.get("function") or {}
                args = fn.get("arguments")
                tc = {"id": c.get("id") or f"call_{jid}_{i}", "type": "function",
                      "function": {"name": fn.get("name"),
                                   "arguments": args if isinstance(args, str) else json.dumps(args or {})}}
                out.append(tc)
                deltas.append({"tool_calls": [{"index": i, **tc}]})
            msg["tool_calls"] = out
            if rescued:
                self.q.add_event(jid, "warning", {"message": f"recovered {len(out)} tool call(s) the model "
                                                             "wrote as text"})
        if deltas:
            self.q.add_event(jid, "deltas", deltas)
        finish = "tool_calls" if calls else (res.get("finish_reason") or "stop")
        return {"message": msg, "finish_reason": finish, "usage": res.get("usage")}

    def complete(self, jid: int, base: str, messages: list, params: dict, *, publish: bool = True) -> dict:
        """Stream one completion from the loaded model. Returns {result, ttft_s, gen_s, n_deltas}.
        publish=False keeps the tokens out of the job's event stream (used by probes)."""
        body = dict(params)
        body.setdefault("max_tokens", self.cfg["default_max_tokens"])
        body.update(model=self.mm.served_name, messages=messages, stream=True)
        req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        since = log_size(self.mm.log_path())         # read only THIS request's log lines on error
        asm, buf = Assembler(), []
        t0 = time.time()
        last_flush = last_check = last_pressure = t0
        warned = False
        ttft, n_deltas = None, 0
        try:
            resp = urllib.request.urlopen(req, timeout=self.cfg["first_token_timeout_s"])
        except urllib.error.HTTPError as e:
            raw = e.read()[:500].decode(errors="replace")
            if e.code >= 500:
                raise explain_server_error(f"HTTP {e.code}: {raw}", self.mm.log_path(), since) from e
            raise ModelError(f"model server HTTP {e.code}: {raw}") from e
        except TimeoutError as e:
            raise ModelError(f"no response from the model for {self.cfg['first_token_timeout_s']}s") from e
        first, ended = True, False
        try:
            with resp:
                for raw in resp:
                    self._touch()
                    if first:
                        _set_read_timeout(resp, self.cfg["idle_token_timeout_s"])
                        first = False
                    now = time.time()
                    if now - last_check >= 0.5:
                        last_check = now
                        self.check(jid)
                    if not warned and now - last_pressure >= self.cfg["pressure_check_s"]:
                        last_pressure = now
                        if self.pressure() == "critical":
                            warned = True
                            self.q.add_event(jid, "warning", {"message": "macOS memory pressure is CRITICAL — "
                                                              "the answer may slow down. Close big apps."})
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        ended = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise explain_server_error(str(chunk["error"]), self.mm.log_path(), since)
                    for ch in chunk.get("choices") or []:
                        d = ch.get("delta") or {}
                        asm.add(d, ch.get("finish_reason"))
                        if d.get("content") or d.get("reasoning_content") or d.get("reasoning") \
                                or d.get("tool_calls"):
                            n_deltas += 1
                            if ttft is None:
                                ttft = now - t0
                        if d and publish:
                            buf.append(d)
                    if chunk.get("usage"):
                        asm.usage = chunk["usage"]
                    if buf and now - last_flush >= 0.25:
                        self.q.add_event(jid, "deltas", buf)
                        buf, last_flush = [], now
        except TimeoutError as e:
            raise ModelError(f"the model stopped sending tokens for {self.cfg['idle_token_timeout_s']}s") from e
        if buf:
            self.q.add_event(jid, "deltas", buf)
        self.check(jid)
        if not ended and asm.finish is None:
            # the connection closed without [DONE] or a finish reason: the model server
            # died or was cut off — a partial answer must never be reported as done
            raise ModelError("the model stopped before finishing its answer (model server crashed?)")
        end = time.time()
        return {"result": asm.result(), "ttft_s": ttft, "gen_s": end - t0 - (ttft or 0),
                "total_s": end - t0, "n_deltas": n_deltas}


    def complete_once(self, jid: int, base: str, messages: list, params: dict) -> dict:
        """One NON-streamed completion (agent turns, request translator, chats with tools).
        The HTTP call runs in a helper thread so cancellation, the watchdog heartbeat and
        the first-token timeout still apply. Returns {result, raw, total_s}."""
        body = dict(params)
        body.setdefault("max_tokens", self.cfg["default_max_tokens"])
        body.update(model=self.mm.served_name, messages=messages, stream=False)
        req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        since = log_size(self.mm.log_path())         # read only THIS request's log lines on error
        box: dict = {}

        def call() -> None:
            try:
                with urllib.request.urlopen(req, timeout=self.cfg["first_token_timeout_s"]) as r:
                    box["raw"] = r.read()
            except urllib.error.HTTPError as e:
                box["http"] = (e.code, e.read()[:800].decode(errors="replace"))
            except Exception as e:  # noqa: BLE001
                box["exc"] = e
        t0 = time.time()
        th = threading.Thread(target=call, daemon=True)
        th.start()
        while th.is_alive():
            self._touch()
            self.check(jid)
            th.join(0.5)
        if "http" in box:
            code, raw = box["http"]
            if code >= 500:
                raise explain_server_error(f"HTTP {code}: {raw}", self.mm.log_path(), since)
            raise ModelError(f"model server HTTP {code}: {raw}")
        if "exc" in box:
            e = box["exc"]
            if isinstance(e, TimeoutError):
                raise ModelError(f"no response from the model for {self.cfg['first_token_timeout_s']}s") from e
            raise ModelError(f"model server unreachable: {e}") from e
        try:
            data = json.loads(box["raw"])
        except (KeyError, json.JSONDecodeError) as e:
            raise ModelError(f"model server sent an unreadable reply: {box.get('raw', b'')[:300]!r}") from e
        if data.get("error"):
            raise explain_server_error(str(data["error"]), self.mm.log_path(), since)
        ch = (data.get("choices") or [{}])[0]
        msg = dict(ch.get("message") or {})
        msg["role"] = "assistant"
        msg["content"] = msg.get("content") or ""
        if msg.get("reasoning") and not msg.get("reasoning_content"):
            msg["reasoning_content"] = msg.pop("reasoning")
        if not msg.get("tool_calls"):
            msg.pop("tool_calls", None)
        res = {"message": msg, "finish_reason": ch.get("finish_reason") or "stop", "usage": data.get("usage")}
        return {"result": res, "raw": data, "total_s": time.time() - t0}


def build(paths=None) -> Worker:
    paths = paths or get_paths()
    reg = load(paths)
    cfg = {**DEFAULTS, **((reg.raw.get("runtime") or {}).get("worker") or {})}
    mm = ModelManager(paths, reg, launcher=MlxOpenAIServerLauncher(paths.runtime / ".venv" / "bin" / "mlx-openai-server"),
                      port=reg.ports["inference"], load_timeout=cfg["load_timeout_s"], ram_wait=cfg["ram_wait_s"])
    return Worker(paths, reg, Queue(paths.queue_db), mm, cfg)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(levelname)s %(message)s")
    w = build()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: w.stop_event.set())
    w.run_forever()


if __name__ == "__main__":
    main()
