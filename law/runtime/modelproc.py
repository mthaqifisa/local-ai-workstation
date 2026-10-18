"""One model in RAM at a time.

ensure(model_id):
  1. refuse models bigger than the registry limit, or not downloaded
  2. same model already running → reuse it
  3. otherwise STOP the current model process (SIGTERM → SIGKILL), wait until its port is
     closed and RAM is actually free, THEN start the new one
     — memory guard: if RAM is still short after waiting AND macOS reports critical memory
       pressure, the load is REFUSED (the Mac would swap itself into a hang); if pressure is
       only normal/warn it loads with a warning (macOS can reclaim cached memory)
  4. wait until it answers /v1/models (watching for a crash while loading)

ensure(model_id, context=N) starts the model with a different context length (used by
`law probe`); the next normal job reloads it with the registry's context.

The model server listens on 127.0.0.1 only: every client goes through the queue (gateway),
so two jobs can never run at once. A pidfile lets a restarted worker kill an orphaned
model process left behind by a crash.

explain_server_error() turns the model server's bare "500" into the real reason by
reading the end of its log (e.g. its 300-second prompt-reading limit).
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import psutil

from ..paths import Paths
from ..registry import Model, Registry, RegistryError

log = logging.getLogger("law.model")
from ..steps.v1 import cache_dir
from . import memory

KIND_TO_TYPE = {"lm": "lm", "vlm": "multimodal", "embed": "embeddings"}
SERVER_PROMPT_LIMIT_S = 300          # mlx-openai-server 1.8.1: internal RPC timeout


class ModelError(RuntimeError):
    retry = True


class ModelRefused(ModelError):
    retry = False


class ModelNotDownloaded(ModelError):
    retry = False


class MemoryBusy(ModelError):
    retry = False


class PromptTooSlow(ModelError):
    """The model server gave up reading a long prompt. Retrying won't help."""
    retry = False


def snapshot_path(paths: Paths, repo: str) -> Path | None:
    d = cache_dir(paths, repo)
    ref = d / "refs" / "main"
    if ref.exists():
        s = d / "snapshots" / ref.read_text().strip()
        if s.is_dir():
            return s
    snaps = d / "snapshots"
    if snaps.is_dir():
        found = sorted((s for s in snaps.iterdir() if s.is_dir()), key=lambda s: s.stat().st_mtime)
        if found:
            return found[-1]
    return None


def tail(path: Path, n: int = 25) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log)"


def log_size(path: Path | None) -> int:
    """Byte length of a log file right now (0 if none) — recorded before a request so the
    error reader can look ONLY at lines written while that request ran."""
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def tail_from(path: Path | None, since: int, n: int = 120) -> str:
    """Log text written after byte offset `since`. Falls back to the last `n` lines if the
    file was rotated/shrank (offset past the end) or nothing was written after it."""
    if not path:
        return ""
    try:
        with path.open("r", errors="replace") as f:
            size = path.stat().st_size
            if 0 < since <= size:
                f.seek(since)
                fresh = f.read()
                if fresh.strip():
                    return "\n".join(fresh.splitlines()[-n:])
    except OSError:
        return "(no log)"
    return tail(path, n)


def explain_server_error(raw: str, logf: Path | None, since: int = 0) -> ModelError:
    """Build the most useful error for a failure the model server reported. `since` is the log
    size captured just before the request, so only THIS request's log lines are read — the
    startup banner (which prints `context_length: 49152`) no longer masks a real crash as
    "prompt too long"."""
    log = tail_from(logf, since, 120) if logf else ""
    prompt = re.findall(r"prompt_tokens=(\d+)", log)
    tokens = f" (~{int(prompt[-1]):,} tokens)" if prompt else ""
    if "TimeoutError" in log or "wait_for" in log:
        return PromptTooSlow(
            f"the model server gave up after {SERVER_PROMPT_LIMIT_S}s reading the prompt{tokens}. "
            "This prompt is too long for this model at this Mac's speed — send less text, split it, "
            "or use a faster model (e.g. the coder). Run `law probe <model>` to see the safe size.")
    # Only a genuine length error — NOT the bare word "context", which appears in ordinary
    # log lines and used to mask real failures (e.g. a chat-template crash) as "too long".
    if re.search(r"maximum context|context length|context window|exceed\w* .*(context|token|length)|"
                 r"too many tokens|input is too long", log, re.I) and prompt:
        return PromptTooSlow(f"the prompt{tokens} is longer than the model's context window. "
                             "Send less text or raise `context` for this model after `law probe`.")
    last = "\n".join(log.splitlines()[-8:]) if log else ""
    return ModelError(f"model error: {raw}" + (f"\nLast model-server log lines:\n{last}" if last else ""))


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _kill_tree(pid: int, wait: float = 30) -> None:
    """SIGTERM the process group, escalate to SIGKILL. Safe if already gone."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
        try:
            psutil.Process(pid).wait(timeout=wait if sig == signal.SIGTERM else 10)
            return
        except psutil.NoSuchProcess:
            return
        except psutil.TimeoutExpired:
            continue


def kill_from_pidfile(pidfile: Path) -> bool:
    """Kill a model process recorded by a previous worker (crash leftovers)."""
    if not pidfile.exists():
        return False
    try:
        info = json.loads(pidfile.read_text())
        proc = psutil.Process(int(info["pid"]))
        cmd = " ".join(proc.cmdline())
        if info.get("marker") and info["marker"] not in cmd:
            pidfile.unlink(missing_ok=True)       # pid reused by something else — leave it
            return False
        _kill_tree(proc.pid)
        killed = True
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, KeyError, json.JSONDecodeError):
        killed = False
    pidfile.unlink(missing_ok=True)
    return killed


class MlxOpenAIServerLauncher:
    """Builds the mlx-openai-server command. Optional flags are only passed if this
    version's `launch --help` lists them, so a CLI change can't break startup."""

    def __init__(self, exe: Path):
        self.exe = Path(exe)
        self._help: str | None = None

    def help(self) -> str:
        if self._help is None:
            r = subprocess.run([str(self.exe), "launch", "--help"], capture_output=True, text=True, timeout=180)
            self._help = (r.stdout or "") + (r.stderr or "")
        return self._help

    def choices(self, flag: str) -> set[str] | None:
        """Allowed values for a flag, parsed from its `[a|b|c]` list in --help (None = any)."""
        m = re.search(re.escape(flag) + r"\s*\[([^\]]+)\]", self.help())
        return {c.strip() for c in m.group(1).split("|")} if m else None

    def __call__(self, m: Model, path: Path, host: str, port: int, context: int | None = None) -> list[str]:
        if not self.exe.exists():
            raise ModelRefused(f"{self.exe} is missing — run ./install.sh")
        h = self.help()
        if "--model-path" not in h:
            raise ModelRefused("this mlx-openai-server version has no `launch --model-path` "
                               "(CLI changed) — run `law logs worker` and report it")
        cmd = [str(self.exe), "launch", "--model-path", str(path), "--model-type", KIND_TO_TYPE[m.kind],
               "--host", host, "--port", str(port)]
        for flag, val in (("--context-length", context or m.context), ("--tool-call-parser", m.tool_parser),
                          ("--reasoning-parser", m.reasoning_parser), ("--max-concurrency", 1)):
            if val is None or flag not in h:
                continue
            allowed = self.choices(flag)
            if allowed is not None and str(val) not in allowed:
                # this server build doesn't know this parser value — passing it makes `launch`
                # exit immediately, so skip it (law splits <think> and recovers tool calls itself)
                log.warning("model %s: %s '%s' is not supported by this mlx-openai-server "
                            "(allowed: %s) — launching without it", m.id, flag, val, ", ".join(sorted(allowed)))
                continue
            cmd += [flag, str(val)]
        if m.tool_parser and "--enable-auto-tool-choice" in h:
            cmd.append("--enable-auto-tool-choice")
        return cmd

    marker = "mlx-openai-server"


class ModelManager:
    def __init__(self, paths: Paths, reg: Registry, *, launcher: Callable, port: int,
                 host: str = "127.0.0.1", load_timeout: float = 1800, ram_wait: float = 90,
                 free_gb: Callable[[], float] | None = None, pressure: Callable[[], str] | None = None):
        self.paths, self.reg, self.launcher = paths, reg, launcher
        self.port, self.host = port, host
        self.load_timeout, self.ram_wait = load_timeout, ram_wait
        self.free_gb = free_gb or memory.available_gb
        self.pressure = pressure or memory.pressure
        self.emit: Callable[[str, dict], None] = lambda t, d: None
        self.tick: Callable[[], None] = lambda: None          # "still making progress" (watchdog)
        self.pidfile = paths.run / "model.pid"
        self.proc: subprocess.Popen | None = None
        self.current: str | None = None
        self.current_ctx: int | None = None
        self.served_name: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def log_path(self, mid: str | None = None) -> Path | None:
        mid = mid or self.current
        return self.paths.logs / f"model-{mid}.log" if mid else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ── public ──────────────────────────────────────────────────────────
    def ensure(self, mid: str, context: int | None = None) -> str:
        try:
            m = self.reg.model(mid)
        except RegistryError as e:
            raise ModelRefused(str(e)) from e
        if m.kind not in KIND_TO_TYPE:
            raise ModelRefused(f"{mid} is a '{m.kind}' model — it can't serve chat")
        fits, why = self.reg.fits(mid)
        if not fits:
            raise ModelRefused(why)
        if self.current == mid and self.current_ctx == context and self.alive():
            return self.base_url
        path = snapshot_path(self.paths, m.repo)
        if path is None:
            raise ModelNotDownloaded(f"{mid} is not downloaded — run: law pull {mid}")

        if self.current:
            self.emit("model", {"state": "unloading", "model": self.current})
        self.stop()
        self._wait_port_free()
        self._wait_ram(m)

        cmd = self.launcher(m, path, self.host, self.port, context=context)
        logf = self.log_path(mid)
        logf.parent.mkdir(parents=True, exist_ok=True)
        self.emit("model", {"state": "loading", "model": mid, "gb": m.weights_gb,
                            **({"context": context} if context else {})})
        t0 = time.time()
        with open(logf, "ab") as fh:
            fh.write(f"\n=== {time.ctime()} start: {' '.join(cmd)}\n".encode())
            fh.flush()
            self.proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         start_new_session=True,
                                         env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONUNBUFFERED": "1"})
        marker = getattr(self.launcher, "marker", Path(cmd[0]).name)
        self.pidfile.parent.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(json.dumps({"pid": self.proc.pid, "model": mid, "marker": marker}))
        self.current, self.current_ctx = mid, context
        try:
            self._wait_ready(logf)
        except BaseException:
            self.stop()
            raise
        self.emit("model", {"state": "ready", "model": mid, "load_s": round(time.time() - t0, 1)})
        return self.base_url

    def stop(self) -> None:
        if self.proc is not None:
            _kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        kill_from_pidfile(self.pidfile)
        self.proc, self.current, self.current_ctx, self.served_name = None, None, None, None

    # ── internals ───────────────────────────────────────────────────────
    def _wait_ready(self, logf: Path) -> None:
        deadline = time.time() + self.load_timeout
        while time.time() < deadline:
            self.tick()
            rc = self.proc.poll()
            if rc is not None:
                raise ModelError(f"model server exited (code {rc}) while loading. Last log lines:\n{tail(logf)}")
            try:
                with urllib.request.urlopen(self.base_url + "/v1/models", timeout=5) as r:
                    ids = [d.get("id") for d in json.loads(r.read()).get("data", [])]
                    self.served_name = ids[0] if ids else self.current
                    return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(1)
        raise ModelError(f"model not ready after {self.load_timeout:.0f}s. Last log lines:\n{tail(logf)}")

    def _wait_port_free(self) -> None:
        for _ in range(60):
            if not port_open(self.port):
                return
            time.sleep(0.5)
        raise ModelError(f"port {self.port} is used by another program — check: lsof -i :{self.port}")

    def _wait_ram(self, m: Model) -> None:
        need = m.weights_gb * 1.05 + 2
        deadline = time.time() + self.ram_wait
        free = self.free_gb()
        if free < need:
            self.emit("model", {"state": "waiting_ram", "model": m.id, "free_gb": round(free, 1),
                                "need_gb": round(need, 1)})
        while free < need and time.time() < deadline:
            self.tick()
            time.sleep(1)
            free = self.free_gb()
        if free >= need:
            return
        if self.pressure() == "critical":
            raise MemoryBusy(f"not loading {m.id}: only {free:.0f} GB RAM free (needs ~{need:.0f} GB) and macOS "
                             "reports CRITICAL memory pressure. Loading now would make the Mac swap and hang. "
                             "Close big apps (browsers, Docker, Xcode), then run the job again.")
        self.emit("warning", {"message": f"only {free:.0f} GB RAM free, {m.id} needs ~{need:.0f} GB — "
                                         "loading anyway (memory pressure is not critical); it may be slow."})
