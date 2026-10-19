"""Live dashboard — watch the workstation think, in a browser.

Runs as launchd service com.law.dashboard on <bind_host>:<dashboard port> (127.0.0.1 by default;
LAW_BIND_HOST=0.0.0.0 in ~/.law/.env opens it to your LAN). No login — instead every request
goes through law.runtime.netguard (trusted networks only, Host + Origin checks, JSON-only writes),
because the dashboard is a command center: it can start agent jobs that run shell commands.
It never runs a job itself — it only reads and writes the queue database, so it shows exactly
what the worker, Telegram and the gateway are all doing — the model loading, each agent step,
the tool calls, and the model's live thinking and answer streaming in token by token.

  GET /              the single-page dashboard (no external files, works offline)
  GET /api/state     machine, worker, queue, recent jobs, ports  (polled every ~2 s)
  GET /api/events    events across all jobs newer than ?since=<ts>  (polled ~2x/s for the stream)

Why a separate service and not part of the gateway: the gateway is the API other programs
depend on; the dashboard is a convenience that reads the same database, so a crash or a slow
browser here can never affect model serving.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..paths import get_paths
from ..registry import load
from . import memory
from .db import Queue
from .netguard import Guard

log = logging.getLogger("law.dashboard")

_MACHINE: dict | None = None


def machine_info() -> dict:
    """Chip / RAM / macOS — read once (they don't change while we run)."""
    global _MACHINE
    if _MACHINE is not None:
        return _MACHINE
    info = {"chip": "this Mac", "ram_gb": round(memory.total_gb()), "os": ""}
    if sys.platform == "darwin":
        def out(args):
            try:
                return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return ""
        info["chip"] = out(["sysctl", "-n", "machdep.cpu.brand_string"]) or info["chip"]
        info["os"] = out(["sw_vers", "-productVersion"])
    _MACHINE = info
    return info


def _port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def job_prompt(job: dict) -> str:
    try:
        p = json.loads(job.get("payload") or "{}")
        what = (p.get("task") or p.get("prompt") or p.get("text") or p.get("query")
                or (p.get("messages") or [{}])[-1].get("content") or "")
    except (ValueError, KeyError, IndexError, TypeError):
        what = ""
    return " ".join(str(what).split())[:140]


def snapshot(paths, reg, q: Queue) -> dict:
    w = q.worker_info()
    now = time.time()
    worker = None
    if w and not (w["state"] == "stopped" or w["age_s"] > 30):
        worker = {"state": w["state"], "model": w.get("model"), "job_id": w.get("job_id"),
                  "progress_age_s": w.get("progress_age_s")}
    jobs = []
    for j in q.jobs(20):
        started = j.get("started") or j.get("created") or now
        jobs.append({"id": j["id"], "status": j["status"], "kind": j.get("kind"),
                     "persona": j.get("persona"), "model": j.get("model"), "source": j.get("source"),
                     "parent_id": j.get("parent_id"), "depth": j.get("depth"),
                     "prompt": job_prompt(j), "error": (j.get("error") or "")[:300],
                     "age_s": round(now - started) if isinstance(started, (int, float)) else None})
    ports = [{"name": n, "port": p, "up": _port_open("127.0.0.1", p)} for n, p in reg.ports.items()]
    personas = [{"name": n, "model": (p or {}).get("model"), "desc": (p or {}).get("desc", "")}
                for n, p in (reg.personas or {}).items()]
    models = []
    try:
        from ..doctor import cached
        for m in reg.models.values():
            models.append({"id": m.id, "label": getattr(m, "label", ""), "gb": getattr(m, "weights_gb", None),
                           "context": getattr(m, "context", None), "kind": getattr(m, "kind", ""),
                           "downloaded": bool(cached(paths, m.repo)), "download": getattr(m, "download", "")})
    except Exception:  # noqa: BLE001 — model list is best-effort
        models = []
    return {"now": now, "machine": machine_info(),
            "mem": {"free_gb": round(memory.available_gb()), "total_gb": round(memory.total_gb()),
                    "pressure": memory.pressure()},
            "worker": worker, "queue": {"depth": q.depth()},
            "jobs": jobs, "ports": ports, "personas": personas, "models": models}


# "Add to Home Screen" on a phone on your LAN → opens full-screen like an app
MANIFEST = {"name": "LAW · Local AI Workstation", "short_name": "LAW", "start_url": "/", "display": "standalone",
            "background_color": "#0b1220", "theme_color": "#0b1220",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" '
        'fill="#0b1220"/><circle cx="256" cy="256" r="150" fill="none" stroke="#0ea5e9" stroke-width="36"/>'
        '<circle cx="256" cy="256" r="52" fill="#38bdf8"/></svg>')


class Handler(BaseHTTPRequestHandler):
    server_version = "law-dashboard"
    q: Queue
    paths = None
    reg = None
    guard: Guard = Guard()

    def log_message(self, *_a):          # don't spam the service log with every poll
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _refused(self, write: bool) -> bool:
        why = self.guard.check(self.client_address[0], self.headers, write=write, json_only=True)
        if why:
            log.warning("refused %s %s from %s: %s", self.command, self.path[:80], self.client_address[0], why)
            self._send(403, json.dumps({"error": why}).encode(), "application/json; charset=utf-8")
        return bool(why)

    def do_GET(self) -> None:
        if self._refused(write=False):
            return
        path = urlparse(self.path)
        try:
            if path.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path.path == "/api/state":
                self._json(snapshot(self.paths, self.reg, self.q))
            elif path.path == "/api/events":
                since = float((parse_qs(path.query).get("since") or ["0"])[0] or 0)
                evs = self.q.recent_events(since, limit=400)
                self._json({"now": time.time(), "events": evs})
            elif path.path == "/api/job":
                jid = int((parse_qs(path.query).get("id") or ["0"])[0] or 0)
                self._json(self._job_detail(jid))
            elif path.path == "/api/job/export":
                from .report import job_markdown
                from .store import Store
                jid = int((parse_qs(path.query).get("id") or ["0"])[0] or 0)
                job = self.q.get(jid) or {}
                cwd = (json.loads(job.get("payload") or "{}") or {}).get("cwd") if job else None
                md = job_markdown(self.q, jid, Store(self.paths.agent_db) if cwd else None, cwd)
                body = md.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="law-job-{jid}.md"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.path == "/media":
                self._serve_media((parse_qs(path.query).get("path") or [""])[0])
            elif path.path == "/api/db":
                self._json(self._db_list())
            elif path.path == "/api/system":
                self._json(self._system())
            elif path.path == "/api/usage":
                self._json(self._usage())
            elif path.path == "/api/gallery":
                self._json(self._gallery())
            elif path.path == "/api/rag/status":
                from . import rag
                self._json({"indexed": rag.status(self.paths)})
            elif path.path == "/api/rag/result":
                jid = int((parse_qs(path.query).get("id") or ["0"])[0] or 0)
                job = self.q.get(jid) or {}
                try:
                    res = json.loads(job.get("result") or "{}")
                except ValueError:
                    res = {}
                self._json({"status": job.get("status"), "results": res.get("results", []),
                            "index": res.get("index")})
            elif path.path == "/manifest.webmanifest":
                self._send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
            elif path.path in ("/icon.svg", "/favicon.ico"):
                self._send(200, ICON.encode(), "image/svg+xml")
            elif path.path == "/health":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001 — a read error must not take the server down
            log.warning("request %s failed: %s", path.path, e)
            self._json({"error": str(e)})

    def do_POST(self) -> None:
        """Interactive actions. The dashboard is a command center: it can start and stop jobs,
        exactly like the CLI or Telegram — every task becomes a normal queue job."""
        if self._refused(write=True):
            return
        path = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        try:
            if path.path == "/api/task":
                self._json(self._enqueue(body))
            elif path.path == "/api/music":
                from ..steps import acestep
                if body.get("action") == "stop":
                    self._json({"ok": True, "note": "stopped" if acestep.stop() else "was not running"})
                else:
                    self._json({"ok": True, "note": acestep.start()})
            elif path.path == "/api/unload":
                w = self.q.worker_info() or {}
                if not w.get("model"):
                    self._json({"ok": True, "note": "no model loaded"})
                else:
                    jid = self.q.enqueue(w["model"], {}, source="dashboard", kind="unload", priority=100)
                    self._json({"ok": True, "id": jid})
            elif path.path == "/api/cancel":
                jid = int(body.get("id"))
                self._json({"ok": bool(self.q.cancel(jid, reason="cancelled from dashboard")), "id": jid})
            elif path.path == "/api/image":
                self._json(self._enqueue_image(body))
            elif path.path == "/api/db/query":
                self._json(self._db_query(body))
            elif path.path == "/api/tts":
                self._json(self._enqueue_tts(body))
            elif path.path == "/api/video":
                self._json(self._enqueue_video(body))
            elif path.path == "/api/vision":
                self._json(self._enqueue_vision(body))
            elif path.path == "/api/rag/index":
                root = (body.get("root") or "").strip()
                if not root:
                    self._json({"error": "give a folder to index"})
                else:
                    jid = self.q.enqueue("qwen3-embed-8b", {"root": root}, source="dashboard", kind="index")
                    self._json({"ok": True, "id": jid})
            elif path.path == "/api/rag/search":
                root = (body.get("root") or "").strip()
                query = (body.get("query") or "").strip()
                if not root or not query:
                    self._json({"error": "need a folder and a query"})
                else:
                    jid = self.q.enqueue("qwen3-embed-8b", {"root": root, "query": query, "k": body.get("k", 8)},
                                         source="dashboard", kind="search")
                    self._json({"ok": True, "id": jid})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa: BLE001
            log.warning("POST %s failed: %s", path.path, e)
            self._json({"error": str(e)})

    def _job_detail(self, jid: int) -> dict:
        """One job as a 'ticket': its fields, its final report, and its full activity timeline
        (thinking, tool calls, delegations) — what you click a card to read, Jira-style."""
        job = self.q.get(jid)
        if not job:
            return {"error": f"no job {jid}"}
        report, plan = "", None
        if job.get("status") == "done":
            try:
                res = json.loads(job.get("result") or "{}")
                report = (res.get("message") or {}).get("content") or ""
                plan = res.get("plan")
            except ValueError:
                report = ""
        try:
            project = (json.loads(job.get("payload") or "{}") or {}).get("cwd")
        except ValueError:
            project = None
        timeline = []
        for e in self.q.events(jid):
            d = e["data"] or {}
            t = e["type"]
            if t == "agent":
                if d.get("think"):
                    timeline.append({"k": "think", "step": d.get("step"), "text": d["think"]})
                elif d.get("say"):
                    timeline.append({"k": "say", "step": d.get("step"), "text": d["say"]})
                elif d.get("note"):
                    timeline.append({"k": "note", "step": d.get("step"), "text": d["note"]})
                elif d.get("tool") == "delegate" and not d.get("output"):
                    timeline.append({"k": "delegate", "step": d.get("step"),
                                     "text": f"→ @{d.get('persona')} (sub-job {d.get('child')})"})
                elif d.get("running"):
                    timeline.append({"k": "tool", "step": d.get("step"),
                                     "text": f"{d.get('tool')} {str(d.get('args',''))[:200]}"})
                elif d.get("tool"):
                    timeline.append({"k": "result", "step": d.get("step"),
                                     "text": (d.get("output") or "")[:200]})
            elif t == "spec":
                timeline.append({"k": "spec", "text": f"{d.get('title','')} → @{d.get('assignee','')}"})
            elif t == "model":
                timeline.append({"k": "model", "text": f"{d.get('state')} {d.get('model') or ''}"})
            elif t == "warning":
                timeline.append({"k": "warn", "text": d.get("message", "")})
            elif t in ("done", "error", "cancelled"):
                timeline.append({"k": t, "text": ""})
        return {"id": jid, "status": job.get("status"), "kind": job.get("kind"),
                "persona": job.get("persona"), "model": job.get("model"), "source": job.get("source"),
                "parent_id": job.get("parent_id"), "error": job.get("error"),
                "prompt": job_prompt(job), "report": report, "timeline": timeline[-200:],
                "plan": plan, "project": project}

    # ── system view (native services + docker, one pane) ─────────────────
    def _system(self) -> dict:
        import os
        reg, uid = self.reg, os.getuid()
        ports = reg.ports

        def loaded(lab):
            try:
                return subprocess.run(["launchctl", "print", f"gui/{uid}/{lab}"],
                                      capture_output=True, timeout=5).returncode == 0
            except Exception:  # noqa: BLE001
                return False
        w = self.q.worker_info()
        worker_up = bool(w and not (w["state"] == "stopped" or w["age_s"] > 30))
        native = [
            {"name": "worker", "up": worker_up and loaded("com.law.worker"),
             "detail": ((w["state"] if w else "stopped") + (f" · {w.get('model')}" if w and w.get("model") else "")) if w else "stopped"},
            {"name": "model server", "up": _port_open("127.0.0.1", ports.get("inference", 8000)),
             "detail": f"MLX :{ports.get('inference', 8000)} · loads on demand"},
            {"name": "gateway", "up": _port_open("127.0.0.1", ports.get("gateway", 4000)),
             "detail": f"OpenAI+Anthropic API :{ports.get('gateway', 4000)}"},
            {"name": "dashboard", "up": True, "detail": f"this page :{ports.get('dashboard', 8800)}"},
            {"name": "watchdog", "up": loaded("com.law.watchdog"), "detail": "restarts frozen services"},
            {"name": "telegram", "up": loaded("com.law.telegram"), "detail": "Telegram bot (if configured)"},
            {"name": "acestep", "up": _port_open("127.0.0.1", ports.get("acestep", 7860)),
             "detail": f"Music studio :{ports.get('acestep', 7860)}"},
        ]
        docker = []
        try:
            env = {**os.environ, "DOCKER_HOST": f"unix://{Path.home()}/.colima/default/docker.sock"}
            r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Ports}}"],
                               env=env, capture_output=True, text=True, timeout=8)
            for line in (r.stdout or "").strip().splitlines():
                p = line.split("\t")
                docker.append({"name": p[0], "up": (len(p) > 1 and p[1] == "running"),
                               "status": p[2] if len(p) > 2 else "", "ports": p[3] if len(p) > 3 else ""})
        except Exception:  # noqa: BLE001
            pass
        mem = {"free_gb": round(memory.available_gb()), "total_gb": round(memory.total_gb()),
               "pressure": memory.pressure()}
        from ..diskinfo import usage
        try:
            disk = usage(self.paths)
        except Exception as e:  # noqa: BLE001 — best effort
            disk = {"error": str(e)}
        return {"native": native, "docker": docker, "mem": mem, "machine": machine_info(), "disk": disk}

    # ── usage / cost analytics ───────────────────────────────────────────
    def _usage(self) -> dict:
        """Aggregate job activity for the Usage view: totals, compute time, and breakdowns by kind,
        model and day. 'compute' = wall-clock the worker spent on jobs (finished-started)."""
        import sqlite3
        c = sqlite3.connect(f"file:{self.paths.queue_db}?mode=ro", uri=True, timeout=5)
        try:
            now = time.time()
            total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            by_status = dict(c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
            dur = "SUM(COALESCE(finished, ?) - started)"
            by_kind = [{"kind": k, "jobs": n, "secs": round(s or 0, 1),
                        "avg": round((s or 0) / n, 1) if n else 0}
                       for k, n, s in c.execute(
                           f"SELECT kind, COUNT(*), {dur} FROM jobs WHERE started IS NOT NULL "
                           "GROUP BY kind ORDER BY 3 DESC", (now,)).fetchall()]
            by_model = [{"model": m, "jobs": n, "secs": round(s or 0, 1)}
                        for m, n, s in c.execute(
                            f"SELECT model, COUNT(*), {dur} FROM jobs WHERE started IS NOT NULL "
                            "GROUP BY model ORDER BY 3 DESC LIMIT 12", (now,)).fetchall()]
            by_day = [{"day": d, "jobs": n} for d, n in c.execute(
                          "SELECT date(created,'unixepoch','localtime') d, COUNT(*) FROM jobs "
                          "WHERE created > ? GROUP BY d ORDER BY d", (now - 14 * 86400,)).fetchall()]
            compute_s = c.execute(f"SELECT {dur} FROM jobs WHERE started IS NOT NULL",
                                  (now,)).fetchone()[0] or 0
            # model health: how often each model loaded, how long that took, its last error
            loads = {m: {"loads": n, "avg_load_s": round(a or 0, 1), "max_load_s": round(mx or 0, 1)}
                     for m, n, a, mx in c.execute(
                         "SELECT json_extract(data,'$.model'), COUNT(*), AVG(json_extract(data,'$.load_s')), "
                         "MAX(json_extract(data,'$.load_s')) FROM events WHERE type='model' "
                         "AND json_extract(data,'$.state')='ready' GROUP BY 1").fetchall() if m}
            for m, n, last in c.execute("SELECT model, COUNT(*), MAX(finished) FROM jobs WHERE status='failed' "
                                        "GROUP BY model").fetchall():
                row = loads.setdefault(m, {"loads": 0, "avg_load_s": 0, "max_load_s": 0})
                row["failed_jobs"] = n
                err = c.execute("SELECT error FROM jobs WHERE model=? AND status='failed' ORDER BY finished DESC "
                                "LIMIT 1", (m,)).fetchone()
                row["last_error"] = ((err[0] if err else "") or "")[:160]
        finally:
            c.close()
        return {"total": total, "by_status": by_status, "by_kind": by_kind, "by_model": by_model,
                "by_day": by_day, "compute_s": round(compute_s, 1), "model_health": loads}

    # ── database viewer (read-only) ──────────────────────────────────────
    def _dbs(self) -> dict:
        return {"queue": self.paths.queue_db, "agent": self.paths.agent_db}

    def _db_ro(self, name: str):
        import sqlite3
        p = self._dbs().get(name)
        if not p or not Path(p).exists():
            return None
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)

    def _db_list(self) -> dict:
        out = []
        for name, p in self._dbs().items():
            info = {"name": name, "path": str(p), "exists": Path(p).exists(), "tables": []}
            c = self._db_ro(name)
            if c:
                try:
                    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                          "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
                        try:
                            n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                        except Exception:  # noqa: BLE001
                            n = None
                        info["tables"].append({"name": t, "rows": n})
                finally:
                    c.close()
            out.append(info)
        return {"databases": out}

    def _db_query(self, body: dict) -> dict:
        import sqlite3
        name = (body.get("db") or "queue").strip()
        sql = (body.get("sql") or "").strip().rstrip(";")
        if not sql:
            return {"error": "empty query"}
        low = sql.lower().lstrip("(")
        if not (low.startswith("select") or low.startswith("pragma") or low.startswith("with")):
            return {"error": "only read-only SELECT / WITH / PRAGMA queries are allowed"}
        if any(k in low for k in (" insert ", " update ", " delete ", " drop ", " alter ", " create ",
                                  " attach ", " replace ")):
            return {"error": "write/DDL statements are not allowed"}
        c = self._db_ro(name)
        if c is None:
            return {"error": f"unknown or missing database '{name}'"}
        try:
            cur = c.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(1000)
            data = [[(v if isinstance(v, (int, float, str, type(None))) else str(v)) for v in r] for r in rows]
            return {"columns": cols, "rows": data, "truncated": len(rows) >= 1000}
        except sqlite3.Error as e:
            return {"error": str(e)}
        finally:
            c.close()

    def _serve_media(self, rel: str) -> None:
        """Serve a generated image or audio file, restricted to the outputs folders (no path escape)."""
        from . import imagegen, ttsgen, videogen
        bases = [imagegen.out_dir(self.paths).resolve(), ttsgen.out_dir(self.paths).resolve(),
                 videogen.out_dir(self.paths).resolve()]
        types = {".png": "image/png", ".jpg": "image/jpeg", ".wav": "audio/wav", ".mp3": "audio/mpeg",
                 ".mp4": "video/mp4"}
        name = Path(rel).name                          # name only — can't climb out
        for base in bases:
            try:
                p = (base / name).resolve()
                if p.parent == base and p.is_file():
                    self._send(200, p.read_bytes(), types.get(p.suffix, "application/octet-stream"))
                    return
            except OSError:
                pass
        self._send(404, b"not found", "text/plain")

    def _gallery(self) -> dict:
        """Recent generated media (images / video / audio) for the Gallery view, newest first."""
        from . import imagegen, ttsgen, videogen
        cats = [("image", imagegen.out_dir(self.paths), (".png", ".jpg", ".jpeg", ".webp")),
                ("video", videogen.out_dir(self.paths), (".mp4", ".mov", ".webm")),
                ("audio", ttsgen.out_dir(self.paths), (".wav", ".mp3", ".m4a", ".flac"))]
        items = []
        for typ, d, exts in cats:
            try:
                for f in d.iterdir():
                    if f.is_file() and f.suffix.lower() in exts:
                        items.append({"type": typ, "name": f.name, "ts": f.stat().st_mtime,
                                      "size": f.stat().st_size})
            except OSError:
                pass
        items.sort(key=lambda x: x["ts"], reverse=True)
        return {"items": items[:120]}

    def _enqueue_image(self, body: dict) -> dict:
        from . import imagegen
        if not imagegen.available(self.paths):
            return {"error": "image generation not installed (mflux missing in model-server venv)"}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return {"error": "empty prompt"}
        params = {k: body[k] for k in ("base_model", "steps", "width", "height", "quantize")
                  if body.get(k) is not None}
        payload = {"prompt": prompt, "params": params, "seed": body.get("seed")}
        jid = self.q.enqueue("flux", payload, persona=None, source="dashboard", kind="image")
        return {"ok": True, "id": jid}

    def _enqueue_video(self, body: dict) -> dict:
        from . import videogen
        if not videogen.available(self.paths):
            return {"error": "video generation not installed (mlx-video missing in model-server venv)"}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return {"error": "empty prompt"}
        params = {}
        if body.get("duration_s") is not None:
            params["duration_s"] = body["duration_s"]
        jid = self.q.enqueue("ltx23", {"prompt": prompt, "params": params, "image": body.get("image")},
                             persona=None, source="dashboard", kind="video")
        return {"ok": True, "id": jid}

    def _enqueue_tts(self, body: dict) -> dict:
        from . import ttsgen
        if not ttsgen.available(self.paths):
            return {"error": "text-to-speech not installed (mlx-audio missing in model-server venv)"}
        text = (body.get("text") or "").strip()
        if not text:
            return {"error": "empty text"}
        params = {"voice": body.get("voice", "af_sky")}
        if body.get("speed") is not None:
            params["speed"] = body["speed"]
        jid = self.q.enqueue("kokoro", {"text": text, "params": params}, persona=None,
                             source="dashboard", kind="tts")
        return {"ok": True, "id": jid}

    def _enqueue_vision(self, body: dict) -> dict:
        from . import visiongen
        if not visiongen.available(self.paths):
            return {"error": "vision not installed (mlx-vlm missing in model-server venv)"}
        b64 = (body.get("image_b64") or "").strip()
        if not b64:
            return {"error": "no image uploaded"}
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            return {"error": "bad image data"}
        if len(raw) > 12_000_000:
            return {"error": "image too large (max 12 MB)"}
        ext = (body.get("ext") or "png").lstrip(".").lower()
        if ext not in ("png", "jpg", "jpeg", "webp", "gif", "heic", "bmp", "tif", "tiff"):
            ext = "png"                                  # never a path piece from the request
        dest = self.paths.outputs / "vision" / f"up-{int(time.time())}.{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        jid = self.q.enqueue("qwen3-vl-8b", {"image": str(dest), "prompt": (body.get("prompt") or "").strip()},
                             persona="vision", source="dashboard", kind="vision")
        return {"ok": True, "id": jid}

    def _enqueue(self, body: dict) -> dict:
        reg = self.reg
        task = (body.get("task") or "").strip()
        if not task:
            return {"error": "empty task"}
        default = (reg.raw.get("defaults") or {}).get("persona", "general")
        persona = body.get("persona") if body.get("persona") in reg.personas else default
        proj = (body.get("project") or "").strip()
        if proj.startswith(("/", "~")):
            project = Path(proj).expanduser()
        elif proj:
            project = self.paths.source / proj
        else:
            project = self.paths.source / "dashboard"
        project = project.resolve()
        model = reg.personas[persona]["model"]
        payload = {"task": task, "cwd": str(project), "depth": 0,
                   "translate": bool(body.get("translate", True)),
                   "messages": [{"role": "user", "content": task}]}
        if body.get("plan_only"):                    # show the spec first, run nothing (like Telegram /plan)
            payload["plan_only"] = True
        elif body.get("spec_text"):                  # run a plan you approved (like Telegram /go)
            payload.update(spec_text=str(body["spec_text"]), translate=False)
        jid = self.q.enqueue(model, payload, persona=persona, source="dashboard", kind="agent")
        return {"ok": True, "id": jid, "project": str(project), "persona": persona, "model": model}


def build(paths=None) -> ThreadingHTTPServer:
    paths = paths or get_paths()
    reg = load(paths)
    port = int(reg.ports.get("dashboard", 8800))
    Handler.q = Queue(paths.queue_db)
    Handler.paths = paths
    Handler.reg = reg
    Handler.guard = Guard()
    httpd = ThreadingHTTPServer((reg.bind_host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s dashboard %(levelname)s %(message)s")
    httpd = build()
    log.info("dashboard on http://%s:%s", httpd.server_address[0], httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


# ── the page ────────────────────────────────────────────────────────────
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>LAW · Local AI Workstation</title>
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="LAW">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#0b1220">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#020817; --surf:#0a1628; --card:rgba(13,30,53,.6); --card2:rgba(13,30,53,.9);
    --bdr:rgba(14,165,233,.14); --bdr2:rgba(14,165,233,.34);
    --blue:#0ea5e9; --blue2:#38bdf8; --purple:#7c3aed; --green:#10b981; --amber:#f59e0b; --red:#ef4444;
    --ink:#e2e8f0; --muted:#94a3b8; --dim:#64748b;
    --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
    --sans:'Space Grotesk',system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:14px;min-height:100vh;
    background-image:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(14,165,233,.08),transparent),
      radial-gradient(ellipse 40% 30% at 90% 0%,rgba(124,58,237,.07),transparent);
    padding-bottom:env(safe-area-inset-bottom,0)}
  a{color:var(--blue);text-decoration:none} a:hover{color:var(--blue2)}
  [hidden]{display:none!important}
  ::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#1b2a44;border-radius:6px}
  /* splash */
  #splash{position:fixed;inset:0;background:var(--bg);z-index:999;display:grid;place-items:center;
    transition:opacity .6s .5s;}
  #splash.gone{opacity:0;pointer-events:none}
  .brand{text-align:center;position:relative}
  .brand .big{font-family:var(--mono);font-weight:700;font-size:72px;letter-spacing:.18em;color:#dbeafe;
    text-shadow:0 0 28px rgba(14,165,233,.9),0 0 60px rgba(14,165,233,.5);animation:glow 2.4s ease-in-out infinite}
  @keyframes glow{50%{text-shadow:0 0 18px rgba(14,165,233,.6),0 0 40px rgba(14,165,233,.3)}}
  .brand .sub{font-family:var(--mono);font-size:12px;letter-spacing:.42em;color:var(--blue);margin-top:10px}
  .bracket{position:fixed;width:46px;height:46px;border:2px solid rgba(14,165,233,.4)}
  .bracket.tl{top:34px;left:34px;border-right:0;border-bottom:0}
  .bracket.tr{top:34px;right:34px;border-left:0;border-bottom:0}
  .bracket.bl{bottom:34px;left:34px;border-right:0;border-top:0}
  .bracket.br{bottom:34px;right:34px;border-left:0;border-top:0}
  /* top nav */
  .nav{display:flex;align-items:center;gap:0;padding:0 20px;height:58px;position:sticky;top:0;z-index:100;
    border-bottom:1px solid var(--bdr);background:rgba(2,8,23,.82);backdrop-filter:blur(14px);
    padding-top:env(safe-area-inset-top,0)}
  .logo{display:flex;align-items:center;gap:10px;margin-right:30px}
  .logo .dot{width:9px;height:9px;border-radius:50%;background:var(--blue);box-shadow:0 0 10px var(--blue);animation:dot 2s infinite}
  @keyframes dot{50%{opacity:.4}}
  .logo .nm{font-family:var(--mono);font-weight:600;font-size:15px;letter-spacing:.09em}
  .links{display:flex;gap:2px;flex:1}
  .links a{font-size:13px;font-weight:500;padding:7px 13px;border-radius:7px;color:var(--muted);cursor:pointer;transition:.15s}
  .links a:hover{color:var(--ink);background:rgba(14,165,233,.1)}
  .links a.active{color:var(--blue);background:rgba(14,165,233,.12)}
  .navstat{display:flex;gap:14px;align-items:center;font-family:var(--mono);font-size:11.5px;color:var(--muted)}
  .navstat b{color:var(--ink);font-weight:600}
  .pres{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;margin-right:5px}
  .pres.on{background:var(--green);box-shadow:0 0 8px var(--green);animation:dot 1.8s infinite}
  .burger{display:none;margin-left:auto;background:none;border:1px solid var(--bdr2);border-radius:7px;color:var(--muted);
    font-size:17px;padding:4px 11px;cursor:pointer}
  @media(max-width:760px){
    .burger{display:block}
    .links{display:none;position:fixed;top:58px;left:0;right:0;flex-direction:column;background:rgba(2,8,23,.98);
      border-bottom:1px solid var(--bdr2);padding:8px;gap:3px;z-index:200;box-shadow:0 10px 30px rgba(0,0,0,.6)}
    .links.open{display:flex}
    .links a{padding:11px 14px;font-size:14px}
    .navstat{display:none}
  }
  main{max-width:1280px;margin:0 auto;padding:20px}
  .view{animation:fade .3s ease}
  @keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .vhead{display:flex;align-items:baseline;gap:12px;margin-bottom:16px}
  .vhead h1{font-family:var(--mono);font-size:18px;font-weight:600;letter-spacing:.05em}
  .vhead .vsub{color:var(--muted);font-size:13px}
  /* cards / grids */
  .card{background:var(--card);border:1px solid var(--bdr);border-radius:14px;padding:16px 18px;backdrop-filter:blur(6px)}
  .card h2{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--muted);margin-bottom:12px}
  .grid{display:grid;gap:16px}
  .kpis{grid-template-columns:repeat(4,1fr)}
  @media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}}
  .kpi{background:var(--card);border:1px solid var(--bdr);border-radius:12px;padding:14px 16px}
  .kpi .l{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim)}
  .kpi .v{font-size:22px;font-weight:600;margin-top:5px}
  .kpi .v small{font-size:12px;color:var(--muted);font-weight:400}
  .cols2{grid-template-columns:1.5fr 1fr;margin-top:16px}
  @media(max-width:900px){.cols2{grid-template-columns:1fr}}
  /* now panel */
  .idle{color:var(--muted);font-style:italic;padding:16px 2px}
  .nowhead{display:flex;align-items:baseline;gap:10px;margin-bottom:4px}
  .nowtitle{font-weight:600;font-size:15px}
  .nowmeta{color:var(--muted);font-size:12px;font-family:var(--mono)}
  .stream{background:#060d1c;border:1px solid var(--bdr);border-radius:10px;padding:12px 14px;margin-top:10px;
    white-space:pre-wrap;word-break:break-word}
  .stream .lbl{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);display:block;margin-bottom:5px}
  .stream.think{border-color:rgba(124,58,237,.3)} .stream.think .b{color:#c4b5fd;font-family:var(--mono);font-size:12.5px}
  .cursor{display:inline-block;width:7px;height:14px;background:var(--blue2);margin-left:2px;vertical-align:-2px;animation:bl 1s steps(2) infinite;border-radius:1px}
  @keyframes bl{50%{opacity:0}}
  .steps{margin-top:12px;display:flex;flex-direction:column;gap:7px;max-height:280px;overflow:auto}
  .step{display:flex;gap:9px;align-items:flex-start;padding:7px 10px;border:1px solid var(--bdr);border-radius:9px;background:var(--card2);animation:fade .25s}
  .step .ic{flex:none;width:20px;text-align:center}
  .step code{font-family:var(--mono);font-size:12px;color:var(--blue2);word-break:break-all}
  .muted{color:var(--muted)} .dim{color:var(--dim)}
  /* badges */
  .badge{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:999px;white-space:nowrap}
  .b-running{background:rgba(14,165,233,.16);color:var(--blue2)}
  .b-done{background:rgba(16,185,129,.15);color:#6ee7b7}
  .b-failed,.b-cancelled{background:rgba(239,68,68,.15);color:#fca5a5}
  .b-queued,.b-waiting{background:rgba(245,158,11,.15);color:#fcd34d}
  .ava{border-radius:8px;display:grid;place-items:center;background:#15233c;color:var(--blue2);font-weight:700;flex:none;width:28px;height:28px;font-size:14px}
  /* kanban */
  .kan{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
  @media(max-width:1100px){.kan{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:560px){.kan{grid-template-columns:1fr}}
  .col{background:rgba(6,13,28,.6);border:1px solid var(--bdr);border-radius:12px;padding:10px;min-height:64px}
  .col h3{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin-bottom:9px;display:flex;justify-content:space-between}
  .col h3 .c{background:#16233a;border-radius:999px;padding:0 8px;color:var(--ink)}
  .tix{display:flex;flex-direction:column;gap:8px}
  .t{background:var(--card2);border:1px solid var(--bdr);border-left:3px solid var(--dim);border-radius:9px;padding:9px 10px;cursor:pointer;transition:.12s}
  .t:hover{border-color:var(--bdr2);transform:translateX(2px)}
  .t.running{border-left-color:var(--blue)} .t.done{border-left-color:var(--green)}
  .t.failed,.t.cancelled{border-left-color:var(--red)} .t.queued,.t.waiting{border-left-color:var(--amber)}
  .t .h{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--muted);font-family:var(--mono)}
  .t .p{font-size:13px;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .t .bar{height:3px;border-radius:2px;background:#16233a;margin-top:7px;overflow:hidden}
  .t .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--green));background-size:200% 100%;animation:flow 2s linear infinite}
  @keyframes flow{to{background-position:200% 0}}
  /* team */
  .team{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
  .mate{border:1px solid var(--bdr);border-radius:14px;padding:15px;background:var(--card);position:relative;transition:.15s}
  .mate:hover{border-color:var(--bdr2)}
  .mate.busy{border-color:var(--bdr2);box-shadow:0 0 26px -12px rgba(14,165,233,.8)}
  .mate .top{display:flex;align-items:center;gap:11px}
  .mate .ava{width:40px;height:40px;font-size:19px;border-radius:11px}
  .mate .nm{font-weight:600;font-size:15px}
  .mate .mdl{font-family:var(--mono);font-size:11px;color:var(--dim)}
  .mate .role{font-size:12.5px;color:var(--muted);margin-top:11px;line-height:1.5}
  .mate .doing{font-size:12.5px;margin-top:12px;padding-top:11px;border-top:1px solid var(--bdr);color:var(--blue2);cursor:pointer}
  .mate .doing.idle{color:var(--dim);cursor:default}
  .mate .cnt{position:absolute;top:14px;right:15px;font-family:var(--mono);font-size:11px;color:var(--dim)}
  /* models */
  .mdlgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
  .mrow{border:1px solid var(--bdr);border-radius:12px;padding:13px 15px;background:var(--card)}
  .mrow .mt{display:flex;align-items:center;gap:9px}
  .mrow .id{font-family:var(--mono);font-weight:600;font-size:13.5px}
  .mrow .meta{color:var(--muted);font-size:12px;margin-top:6px;display:flex;gap:14px;flex-wrap:wrap;font-family:var(--mono)}
  .chip{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;padding:2px 7px;border-radius:6px}
  .chip.dl{background:rgba(16,185,129,.14);color:#6ee7b7} .chip.no{background:rgba(100,116,139,.18);color:var(--muted)}
  /* studio / launchers */
  .launch{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
  .tile{border:1px solid var(--bdr);border-radius:14px;padding:18px;background:var(--card);display:block;transition:.14s;position:relative;overflow:hidden}
  .tile:hover{transform:translateY(-3px);border-color:var(--bdr2);box-shadow:0 12px 30px -16px rgba(14,165,233,.6)}
  .tile .ico{font-size:26px} .tile .n{font-weight:600;font-size:15px;margin-top:10px}
  .tile .d{font-size:12.5px;color:var(--muted);margin-top:4px}
  .tile.off{opacity:.6} .tile.off:hover{transform:none;box-shadow:none}
  .soon{position:absolute;top:12px;right:12px;font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:.12em;
    color:var(--amber);border:1px solid rgba(245,158,11,.35);border-radius:6px;padding:2px 6px}
  .okdot{position:absolute;top:14px;right:14px;width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green)}
  /* chat view */
  .chatwrap{display:flex;flex-direction:column;gap:14px}
  .chatbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .chatbar select,.chatbar input{background:var(--card2);border:1px solid var(--bdr);color:var(--ink);border-radius:9px;padding:10px 12px;font-family:var(--sans);font-size:13.5px}
  .chatbar input.proj{width:200px}
  .inputrow{display:flex;gap:10px;align-items:flex-end}
  .inputrow textarea{flex:1;background:var(--card2);border:1px solid var(--bdr);color:var(--ink);border-radius:12px;padding:13px 15px;
    font-family:var(--sans);font-size:14px;resize:none;min-height:52px;max-height:160px}
  .inputrow textarea:focus{outline:none;border-color:var(--bdr2)}
  .btn{background:linear-gradient(180deg,#0ea5e9,#0284c7);border:0;color:#fff;font-weight:600;border-radius:10px;padding:12px 20px;cursor:pointer;font-size:14px}
  .btn:hover{filter:brightness(1.1)} .btn:disabled{opacity:.5;cursor:default}
  .btn.ghost{background:transparent;border:1px solid var(--bdr2);color:var(--muted);padding:5px 12px;font-weight:500}
  .btn.ghost:hover{color:var(--ink)}
  /* drawer */
  .scrim{position:fixed;inset:0;background:rgba(2,8,23,.6);opacity:0;pointer-events:none;transition:.2s;z-index:300}
  .scrim.show{opacity:1;pointer-events:auto}
  .drawer{position:fixed;top:0;right:0;height:100%;width:min(580px,95vw);background:var(--surf);border-left:1px solid var(--bdr2);
    transform:translateX(100%);transition:transform .25s;z-index:301;display:flex;flex-direction:column;padding-top:env(safe-area-inset-top,0)}
  .drawer.show{transform:none}
  .dh{padding:17px 20px;border-bottom:1px solid var(--bdr);display:flex;align-items:flex-start;gap:11px}
  .dh .x{margin-left:auto;cursor:pointer;color:var(--muted);font-size:20px;line-height:1}
  .db{overflow:auto;padding:16px 20px}
  .report{background:#060d1c;border:1px solid var(--bdr);border-radius:11px;padding:13px;margin-bottom:14px;white-space:pre-wrap;word-break:break-word;font-size:13px}
  .subtree{margin-bottom:14px}
  .subtree .s{font-size:12.5px;padding:7px 9px;border-radius:8px;background:var(--card2);margin-top:6px;cursor:pointer;border-left:3px solid var(--blue)}
  .subtree .s:hover{background:#13223c}
  .tl{display:flex;flex-direction:column;gap:9px}
  .ev{display:flex;gap:10px;font-size:12.5px}.ev .ic{flex:none;width:18px;text-align:center}
  .ev .tx{min-width:0;white-space:pre-wrap;word-break:break-word}
  .ev.think .tx{color:#c4b5fd;font-family:var(--mono);font-size:12px}
  .ev.warn .tx{color:var(--amber)} .ev.result .tx{color:var(--muted);font-family:var(--mono);font-size:12px}
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--card2);border:1px solid var(--bdr2);
    border-radius:11px;padding:11px 18px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;z-index:400}
  .toast.show{opacity:1}
  /* create + data controls */
  .sec-lbl{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:var(--dim);margin:14px 0 7px}
  .chiprow{display:flex;flex-wrap:wrap;gap:7px}
  .opt{background:var(--card2);border:1px solid var(--bdr);color:var(--muted);border-radius:8px;padding:7px 12px;
       font-size:12.5px;cursor:pointer;transition:.12s;font-family:var(--sans)}
  .opt:hover{color:var(--ink);border-color:var(--bdr2)}
  .opt.on{background:rgba(14,165,233,.14);border-color:var(--blue);color:var(--blue2)}
  .ta{width:100%;min-height:72px;background:var(--card2);border:1px solid var(--bdr);color:var(--ink);border-radius:11px;
      padding:11px 13px;font-family:var(--sans);font-size:13.5px;resize:vertical}
  .ta:focus{outline:none;border-color:var(--bdr2)}
  input[type=range]{accent-color:var(--blue)}
  .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px}
  .gallery img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;border:1px solid var(--bdr);cursor:pointer}
  .dtable{width:100%;border-collapse:collapse;font-size:12px;font-family:var(--mono)}
  .dtable th{text-align:left;color:var(--blue2);border-bottom:1px solid var(--bdr2);padding:6px 9px;position:sticky;top:0;background:var(--surf)}
  .dtable td{padding:5px 9px;border-bottom:1px solid var(--bdr);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .dbtbl{display:flex;flex-direction:column;gap:5px;margin-top:6px}
  .dbtbl .tb{display:flex;justify-content:space-between;padding:6px 10px;border:1px solid var(--bdr);border-radius:8px;
             background:var(--card2);cursor:pointer;font-size:12.5px}
  .dbtbl .tb:hover{border-color:var(--blue)} .dbtbl .tb .rows{color:var(--dim);font-family:var(--mono);font-size:11px}
  .dbname{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:12px 0 2px}
  .svc{display:flex;align-items:center;gap:11px;padding:9px 11px;border:1px solid var(--bdr);border-radius:9px;background:var(--card2);margin-bottom:7px}
  .svc-dot{width:9px;height:9px;border-radius:50%;flex:none}
  .svc-nm{font-weight:600;font-size:13.5px}
</style></head>
<body>
<div id="splash"><span class="bracket tl"></span><span class="bracket tr"></span><span class="bracket bl"></span><span class="bracket br"></span>
  <div class="brand"><div class="big">LAW</div><div class="sub">LOCAL AI WORKSTATION</div></div></div>

<header class="nav">
  <a class="logo" onclick="go('dashboard')"><span class="dot"></span><span class="nm">LAW</span></a>
  <nav class="links" id="links">
    <a data-v="dashboard" class="active" onclick="go('dashboard')">Dashboard</a>
    <a data-v="chat" onclick="go('chat')">Chat</a>
    <a data-v="create" onclick="go('create')">Create</a>
    <a data-v="gallery" onclick="go('gallery')">Gallery</a>
    <a data-v="search" onclick="go('search')">Search</a>
    <a data-v="team" onclick="go('team')">Team</a>
    <a data-v="models" onclick="go('models')">Models</a>
    <a data-v="data" onclick="go('data')">Data</a>
    <a data-v="system" onclick="go('system')">System</a>
    <a data-v="usage" onclick="go('usage')">Usage</a>
    <a data-v="studio" onclick="go('studio')">Studio</a>
  </nav>
  <div class="navstat">
    <span><span class="pres" id="np"></span><span id="nw">worker</span></span>
    <span id="nm">—</span>
  </div>
  <button class="burger" onclick="document.getElementById('links').classList.toggle('open')">☰</button>
</header>

<main>
  <!-- DASHBOARD -->
  <section class="view" id="view-dashboard">
    <div class="vhead"><h1>Command center</h1><span class="vsub">every task, every agent, live</span></div>
    <div class="grid kpis" id="kpis"></div>
    <div class="grid cols2">
      <section class="card"><h2>Now — live</h2><div id="now"><div class="idle">No agent working. Dispatch from Chat, or send Tony a message on Telegram.</div></div></section>
      <section class="card"><h2>Active agents</h2><div class="team" id="lanes" style="grid-template-columns:1fr"></div></section>
    </div>
    <section class="card" style="margin-top:16px"><h2>Task board</h2><div class="kan" id="kan"></div></section>
  </section>

  <!-- CHAT -->
  <section class="view" id="view-chat" hidden>
    <div class="vhead"><h1>Chat / dispatch</h1><span class="vsub">give an agent a task and watch it think</span></div>
    <div class="chatwrap">
      <div class="chatbar">
        <select id="c-persona" title="agent"></select>
        <input class="proj" id="c-proj" placeholder="project folder (optional)" autocomplete="off">
        <label class="muted" style="font-size:12px;font-family:var(--mono);display:flex;align-items:center;gap:6px;cursor:pointer" title="the agent writes a task spec first; you read it, then press Run this plan"><input type="checkbox" id="c-plan"> plan first</label>
        <span class="muted" style="font-size:12px;font-family:var(--mono)">agent mode · tools · web · files</span>
      </div>
      <section class="card" style="min-height:220px"><h2>Live</h2><div id="cnow"><div class="idle">Your dispatched task appears here, thinking out loud.</div></div></section>
      <div class="inputrow">
        <textarea id="c-task" placeholder="Describe a task…  (Enter to dispatch, Shift+Enter for newline)"></textarea>
        <button class="btn" id="c-run">Dispatch ▶</button>
      </div>
    </div>
  </section>

  <!-- CREATE (image) -->
  <section class="view" id="view-create" hidden>
    <div class="vhead"><h1>Create</h1><span class="vsub">generate images locally — no API keys</span></div>
    <div class="grid cols2" style="margin-top:0">
      <section class="card">
        <h2>Settings</h2>
        <div class="sec-lbl">Model</div>
        <div id="cr-models" class="chiprow"></div>
        <div class="dim" id="cr-modelnote" style="font-size:11.5px;margin:4px 0 12px"></div>
        <div class="sec-lbl">Prompt</div>
        <textarea id="cr-prompt" class="ta" placeholder="a futuristic city skyline at sunset with flying cars, cinematic lighting, 8k detail…"></textarea>
        <div class="sec-lbl">Style</div>
        <div id="cr-styles" class="chiprow"></div>
        <div class="sec-lbl">Aspect</div>
        <div id="cr-res" class="chiprow"></div>
        <div class="sec-lbl">Steps · <span id="cr-stepsv" style="color:var(--blue2)">9</span></div>
        <input type="range" id="cr-steps" min="1" max="30" value="9" style="width:100%"
          oninput="document.getElementById('cr-stepsv').textContent=this.value">
        <div class="sec-lbl">Seed</div>
        <div style="display:flex;gap:8px"><input id="cr-seed" class="proj" placeholder="random" style="flex:1;width:auto">
          <button class="btn ghost" onclick="document.getElementById('cr-seed').value=Math.floor(Math.random()*1e9)">⚄ random</button></div>
        <div class="sec-lbl" style="cursor:pointer" onclick="var e=document.getElementById('cr-adv');e.hidden=!e.hidden">▸ Advanced (negative prompt)</div>
        <div id="cr-adv" hidden><textarea id="cr-neg" class="ta" placeholder="things to avoid: blurry, watermark, deformed, text…" style="min-height:48px"></textarea></div>
        <button class="btn" id="cr-run" style="width:100%;margin-top:14px">✦ Generate</button>
      </section>
      <section class="card">
        <h2>Result</h2>
        <div id="cr-out"><div class="idle">Your generated image appears here. First run of a model downloads its weights (a few minutes).</div></div>
        <h2 style="margin-top:18px">Recent</h2>
        <div id="cr-gallery" class="gallery"></div>
      </section>
    </div>
  </section>

  <!-- SEARCH (RAG) -->
  <section class="view" id="view-search" hidden>
    <div class="vhead"><h1>Search</h1><span class="vsub">semantic search over your files (local embeddings)</span></div>
    <section class="card">
      <div class="sec-lbl">Folder</div>
      <input id="rag-root" class="proj" placeholder="~/SourceCode/my-project" style="width:100%" autocomplete="off">
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn ghost" id="rag-index">Index this folder</button>
        <span class="dim" id="rag-indexed" style="font-size:12px;align-self:center"></span>
      </div>
      <div class="sec-lbl">Query</div>
      <div style="display:flex;gap:8px"><input id="rag-q" class="proj" placeholder="what are you looking for?" style="flex:1;width:auto" autocomplete="off">
        <button class="btn" id="rag-run">Search</button></div>
      <div id="rag-out" style="margin-top:14px"></div>
    </section>
  </section>

  <!-- TEAM -->
  <section class="view" id="view-team" hidden>
    <div class="vhead"><h1>Team</h1><span class="vsub">your specialist agents</span></div>
    <div class="team" id="team"></div>
  </section>

  <!-- MODELS -->
  <section class="view" id="view-models" hidden>
    <div class="vhead"><h1>Models</h1><span class="vsub">local model registry</span></div>
    <div class="mdlgrid" id="models"></div>
  </section>

  <!-- DATA (db viewer) -->
  <section class="view" id="view-data" hidden>
    <div class="vhead"><h1>Data</h1><span class="vsub">browse the SQLite databases — read-only</span></div>
    <div class="grid cols2" style="margin-top:0">
      <section class="card">
        <h2>Databases &amp; tables</h2>
        <div id="db-tables"><div class="idle">loading…</div></div>
      </section>
      <section class="card">
        <h2>Query <span class="dim" id="db-active" style="font-family:var(--mono);font-size:11px"></span></h2>
        <textarea id="db-sql" class="ta" placeholder="SELECT * FROM jobs ORDER BY id DESC LIMIT 50" style="font-family:var(--mono);font-size:12.5px"></textarea>
        <div style="display:flex;gap:8px;margin-top:8px"><button class="btn" id="db-run">Run (read-only)</button>
          <span class="dim" id="db-info" style="font-size:12px;align-self:center"></span></div>
        <div id="db-out" style="margin-top:12px;overflow:auto"></div>
      </section>
    </div>
  </section>

  <!-- SYSTEM -->
  <section class="view" id="view-system" hidden>
    <div class="vhead"><h1>System</h1><span class="vsub">native services + Docker containers, one pane</span>
      <button class="btn" style="margin-left:auto" onclick="unloadModel()" title="free the RAM now; the model reloads on the next job">⏏ Unload model</button></div>
    <div class="grid cols2" style="margin-top:0">
      <section class="card">
        <h2>Native services <span class="dim" style="font-size:11px">(run on macOS — GPU/MLX; not in Portainer)</span></h2>
        <div id="sys-native"><div class="idle">loading…</div></div>
      </section>
      <section class="card">
        <h2>Docker containers <span class="dim" style="font-size:11px">(also in Portainer :9000)</span></h2>
        <div id="sys-docker"><div class="idle">loading…</div></div>
      </section>
      <section class="card">
        <h2>Disk <span class="dim" style="font-size:11px">(what law uses · free space with <code>law reclaim</code>)</span></h2>
        <div id="sys-disk"><div class="idle">loading…</div></div>
      </section>
    </div>
  </section>

  <!-- GALLERY -->
  <section class="view" id="view-gallery" hidden>
    <div class="vhead"><h1>Gallery</h1><span class="vsub">everything you've generated — images, video, audio</span></div>
    <div id="gallery-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px"><div class="idle">loading…</div></div>
  </section>

  <!-- USAGE -->
  <section class="view" id="view-usage" hidden>
    <div class="vhead"><h1>Usage</h1><span class="vsub">jobs &amp; compute time — what this workstation has done</span></div>
    <div class="grid kpis" id="usage-kpis" style="margin-bottom:16px"></div>
    <div class="grid cols2" style="margin-top:0">
      <section class="card"><h2>Last 14 days</h2><div id="usage-days"><div class="idle">loading…</div></div></section>
      <section class="card"><h2>By kind</h2><div id="usage-kind"><div class="idle">loading…</div></div></section>
    </div>
    <section class="card" style="margin-top:16px"><h2>By model</h2><div id="usage-model"><div class="idle">loading…</div></div></section>
  </section>

  <!-- STUDIO -->
  <section class="view" id="view-studio" hidden>
    <div class="vhead"><h1>Studio &amp; services</h1><span class="vsub">image generation is on the Create tab</span></div>
    <section class="card" style="margin-bottom:16px">
      <h2>👁 Vision — analyse an image (Qwen3-VL)</h2>
      <div class="sec-lbl">Image</div>
      <input type="file" id="vis-file" accept="image/*" style="width:100%;padding:10px;background:var(--panel);border:1px solid var(--line);border-radius:10px;color:var(--ink)">
      <div class="sec-lbl">Question (optional)</div>
      <input id="vis-prompt" class="ta" style="padding:10px" placeholder="e.g. what is this? where is this place? transcribe the text">
      <button class="btn" id="vis-run" style="margin-top:12px">👁 Analyse image</button>
      <div id="vis-out" style="margin-top:12px"></div>
    </section>
    <section class="card" style="margin-bottom:16px">
      <h2>🔊 Voice — text to speech (Kokoro)</h2>
      <div class="sec-lbl">Voice</div>
      <div id="tts-voices" class="chiprow"></div>
      <div class="sec-lbl">Text</div>
      <textarea id="tts-text" class="ta" placeholder="Type something to say aloud…"></textarea>
      <button class="btn" id="tts-run" style="margin-top:12px">🔊 Speak</button>
      <div id="tts-out" style="margin-top:12px"></div>
    </section>
    <section class="card" style="margin-bottom:16px">
      <h2>🎬 Video — text to video (LTX-2.3)</h2>
      <div class="sec-lbl">Prompt</div>
      <textarea id="vid-prompt" class="ta" placeholder="a drone shot flying over a misty mountain forest at sunrise, cinematic…"></textarea>
      <div class="sec-lbl">Duration</div>
      <div id="vid-dur" class="chiprow"></div>
      <button class="btn" id="vid-run" style="margin-top:12px">🎬 Generate video</button>
      <div class="dim" style="font-size:11.5px;margin-top:6px">First run downloads LTX-2.3 (~21 GB). ~45s per 5s clip after that.</div>
      <div id="vid-out" style="margin-top:12px"></div>
    </section>
    <h2 style="font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--muted);margin-bottom:12px">Services</h2>
    <div class="launch" id="launch"></div>
  </section>
</main>

<div class="scrim" id="scrim" onclick="closeJob()"></div>
<aside class="drawer" id="drawer"><div class="dh" id="dh"></div><div class="db" id="db"></div></aside>
<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>(s==null?"":String(s)).replace(/[&<>]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[m]));
const AVA={orchestrator:"🧭",coder:"🛠",general:"🤖",qa:"🔍",researcher:"📚",reasoner:"🧠","deep-reasoner":"🧠",writer:"✍",ba:"📋",vision:"🖼",designer:"🎨"};
let state=null, since=0; const live={};
function ago(s){if(s==null)return"";s=Math.round(s);if(s<60)return s+"s";if(s<3600)return Math.round(s/60)+"m";return Math.round(s/3600)+"h";}
function liveSplit(raw){raw=raw||"";const i=raw.indexOf("</think>");if(i>=0)return[raw.slice(0,i).replaceAll("<think>","").trim(),raw.slice(i+8).trim()];return[raw.replaceAll("<think>","").trim(),""];}

/* routing */
function go(v){location.hash=v;}
function route(){
  const v=(location.hash||"#dashboard").slice(1);
  document.querySelectorAll(".view").forEach(s=>s.hidden=(s.id!=="view-"+v));
  document.querySelectorAll("#links a").forEach(a=>a.classList.toggle("active",a.dataset.v===v));
  document.getElementById("links").classList.remove("open");
  render();
}
window.addEventListener("hashchange",route);

/* polling */
async function poll(){
  try{const j=await (await fetch("/api/events?since="+since,{cache:"no-store"})).json();(j.events||[]).forEach(applyEvent);since=j.now||since;}catch(e){}
}
function L(id){return live[id]||(live[id]={stream:"",think:"",steps:[]});}
function applyEvent(ev){
  const d=ev.data||{},id=ev.job_id,l=L(id);
  if(ev.type==="deltas"){(Array.isArray(d)?d:[]).forEach(x=>{if(x.reasoning_content||x.reasoning)l.think+=(x.reasoning_content||x.reasoning);if(x.content)l.stream+=x.content;});if(l.stream.length>7000)l.stream=l.stream.slice(-6000);}
  else if(ev.type==="model"){const s=d.state,m=d.model;if(s==="loading")l.steps.push({ic:"⬇",tx:"loading <b>"+esc(m)+"</b>…"});else if(s==="ready")l.steps.push({ic:"✓",tx:"<b>"+esc(m)+"</b> ready "+esc(d.load_s)+"s"});}
  else if(ev.type==="spec"){l.stream="";l.think="";l.steps.push({ic:"📝",tx:"spec: <b>"+esc(d.title||"")+"</b> → @"+esc(d.assignee||"")});}
  else if(ev.type==="agent"){l.stream="";l.think="";
    if(d.think)l.steps.push({ic:"🧠",tx:'<span class="dim">step '+esc(d.step)+'</span> '+esc(d.think)});
    else if(d.say)l.steps.push({ic:"💬",tx:'<span class="dim">step '+esc(d.step)+'</span> '+esc(d.say)});
    else if(d.note)l.steps.push({ic:"·",tx:'<span class="dim">'+esc(d.note)+'</span>'});
    else if(d.tool==="delegate")l.steps.push({ic:"→",tx:d.output?("sub-job "+esc(d.child)+" ended"):("delegate → @"+esc(d.persona)+" (#"+esc(d.child)+")")});
    else if(d.running)l.steps.push({ic:"🔧",tx:"<b>"+esc(d.tool)+"</b> <code>"+esc((d.args||"").slice(0,150))+"</code>"});
    else if(d.tool)l.steps.push({ic:"↳",tx:'<span class="dim">'+esc((d.output||"done").split("\n")[0].slice(0,150))+'</span>'});}
  else if(ev.type==="warning")l.steps.push({ic:"⚠",tx:'<span style="color:var(--amber)">'+esc(d.message)+'</span>'});
  else if(ev.type==="done"||ev.type==="error"||ev.type==="cancelled"){l.done=ev.type;l.stream="";l.think="";}
  if(l.steps.length>60)l.steps=l.steps.slice(-60);
}
function activeJob(){if(!state)return null;const w=state.worker;if(w&&w.job_id&&w.state==="running")return w.job_id;const r=(state.jobs||[]).find(j=>j.status==="running");return r?r.id:((state.jobs||[])[0]||{}).id;}

/* renderers */
function nowHTML(targetRunning){
  const jid=activeJob(),job=state&&(state.jobs||[]).find(j=>j.id===jid);
  if(!job)return null;
  const l=L(jid),[think,ans]=liveSplit(l.stream),thinking=((l.think?l.think+" ":"")+think).trim();
  const run=state.worker&&state.worker.state==="running",cur=run?'<span class="cursor"></span>':'';
  let h='<div class="nowhead"><span class="nowtitle">#'+job.id+' '+esc(job.prompt||job.kind||"")+'</span></div>'+
    '<div class="nowmeta">@'+esc(job.persona||"")+' · '+esc(job.model||"")+' · <span class="badge b-'+job.status+'">'+job.status+'</span></div>';
  if(thinking)h+='<div class="stream think"><span class="lbl">🧠 thinking</span><span class="b">'+esc(thinking.slice(-1400))+(ans?"":cur)+'</span></div>';
  if(ans)h+='<div class="stream"><span class="lbl">💬 answer</span><span class="b">'+esc(ans.slice(-1400))+cur+'</span></div>';
  if(l.steps.length)h+='<div class="steps">'+l.steps.slice(-40).map(s=>'<div class="step"><span class="ic">'+s.ic+'</span><span>'+s.tx+'</span></div>').join("")+'</div>';
  return h;
}
function renderNow(){const h=nowHTML();["#now","#cnow"].forEach(sel=>{const el=$(sel);if(!el)return;if(h){el.innerHTML=h;const sc=el.querySelector(".steps");if(sc)sc.scrollTop=sc.scrollHeight;}});}
function renderKPIs(){if(!state)return;const m=state.mem||{},w=state.worker,q=state.queue||{};
  const act=(state.jobs||[]).filter(j=>["running","queued","waiting"].includes(j.status)).length;
  const done=(state.jobs||[]).filter(j=>j.status==="done").length;
  $("#kpis").innerHTML=[
    ["Worker",(w?w.state:"idle")+(w&&w.model?' <small>'+esc(w.model)+'</small>':'')],
    ["Active tasks",act+' <small>'+q.depth+' queued</small>'],
    ["Completed",done],
    ["Memory",m.free_gb+' <small>/ '+m.total_gb+' GB · '+esc(m.pressure||"")+'</small>'],
  ].map(k=>'<div class="kpi"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join("");
}
const COLS=[["queued","Queued"],["running","Running"],["waiting","Waiting"],["done","Done"],["failed","Failed"]];
function renderKanban(){const kan=$("#kan");if(!kan||!state)return;const g={};COLS.forEach(([k])=>g[k]=[]);
  (state.jobs||[]).forEach(j=>{const k=g[j.status]?j.status:(j.status==="cancelled"?"failed":"done");(g[k]||g.done).push(j);});
  kan.innerHTML=COLS.map(([k,label])=>{const js=g[k]||[];const l=id=>live[id]||{steps:[]};
    const cards=js.map(j=>{const stepN=l(j.id).steps.filter(s=>s.ic==="🔧").length;const pct=j.status==="done"?100:j.status==="queued"?6:j.status==="waiting"?50:Math.min(92,18+stepN*9);
      return '<div class="t '+k+'" onclick="openJob('+j.id+')"><div class="h"><span class="ava" style="width:18px;height:18px;font-size:11px">'+(AVA[j.persona]||"🤖")+'</span>#'+j.id+(j.parent_id?' ↳'+j.parent_id:'')+' · @'+esc(j.persona||j.model||'')+' · '+ago(j.age_s)+'</div><div class="p">'+esc(j.prompt||("("+(j.kind||'job')+")"))+'</div>'+(["running","waiting"].includes(j.status)?'<div class="bar"><i style="width:'+pct+'%"></i></div>':'')+'</div>';}).join("");
    return '<div class="col"><h3>'+label+'<span class="c">'+js.length+'</span></h3><div class="tix">'+(cards||'<div class="dim" style="font-size:12px">—</div>')+'</div></div>';}).join("");
}
function renderLanes(){const box=$("#lanes");if(!box||!state)return;const act=(state.jobs||[]).filter(j=>["running","waiting","queued"].includes(j.status));
  if(!act.length){box.innerHTML='<div class="idle">idle — no agents working</div>';return;}
  box.innerHTML=act.slice(0,8).map(j=>{const l=live[j.id]||{steps:[]};const last=l.steps.length?l.steps[l.steps.length-1].tx.replace(/<[^>]+>/g,""):j.status;
    return '<div class="mate '+(j.status==="running"?"busy":"")+'" onclick="openJob('+j.id+')" style="cursor:pointer;padding:11px"><div class="top"><span class="ava" style="width:30px;height:30px;font-size:14px">'+(AVA[j.persona]||"🤖")+'</span><div style="min-width:0"><div class="nm" style="font-size:13.5px">@'+esc(j.persona||j.model||"agent")+' <span class="badge b-'+j.status+'">'+j.status+'</span></div><div class="dim" style="font-size:11.5px;font-family:var(--mono)">#'+j.id+(j.parent_id?' · sub of #'+j.parent_id:'')+'</div></div></div><div class="role" style="margin-top:8px">'+esc((last||"").slice(0,90))+'</div></div>';}).join("");
}
function renderTeam(){const box=$("#team");if(!box||!state||!state.personas)return;const jobs=state.jobs||[];
  box.innerHTML=state.personas.map(p=>{const mine=jobs.filter(j=>j.persona===p.name);const act=mine.find(j=>["running","waiting"].includes(j.status))||mine.find(j=>j.status==="queued");const done=mine.filter(j=>j.status==="done").length;const busy=act&&["running","waiting"].includes(act.status);
    const doing=act?'<div class="doing" onclick="openJob('+act.id+')"><span class="pres '+(busy?"on":"")+'"></span>'+(act.status==="queued"?"queued: ":"working: ")+esc((act.prompt||"").slice(0,54))+'</div>':'<div class="doing idle"><span class="pres"></span>idle</div>';
    return '<div class="mate '+(busy?"busy":"")+'"><span class="cnt">'+(done?done+" done":"")+'</span><div class="top"><span class="ava">'+(AVA[p.name]||"🤖")+'</span><div style="min-width:0"><div class="nm">@'+esc(p.name)+'</div><div class="mdl">'+esc(p.model||"")+'</div></div></div><div class="role">'+esc(p.desc||"")+'</div>'+doing+'</div>';}).join("");
}
function renderModels(){const box=$("#models");if(!box||!state)return;const ms=state.models||[];if(!ms.length){box.innerHTML='<div class="idle">no models listed</div>';return;}
  box.innerHTML=ms.map(m=>'<div class="mrow"><div class="mt"><span class="id">'+esc(m.id)+'</span><span class="chip '+(m.downloaded?"dl":"no")+'">'+(m.downloaded?"downloaded":"not local")+'</span></div><div class="dim" style="font-size:12px;margin-top:4px">'+esc(m.label||m.kind||"")+'</div><div class="meta"><span>'+(m.gb||"?")+' GB</span><span>ctx '+(m.context||"—")+'</span><span>'+esc(m.download||"")+'</span></div></div>').join("");
}
function renderLaunchers(){const box=$("#launch");if(!box||!state)return;const h=location.hostname||"127.0.0.1";const by={};(state.ports||[]).forEach(p=>by[p.name]=p);const up=n=>by[n]&&by[n].up;
  const tile=(name,port,ico,label,desc,soon)=>{const on=!soon&&up(name);const href=on?"http://"+h+":"+port:null;const tag=soon?'<span class="soon">phase 6</span>':(on?'<span class="okdot"></span>':'');
    const inner='<div class="ico">'+ico+'</div><div class="n">'+label+'</div><div class="d">'+(on?"open · :"+port:(soon?desc:"offline · :"+port))+'</div>'+tag;
    return href?'<a class="tile" href="'+href+'" target="_blank">'+inner+'</a>':'<div class="tile off">'+inner+'</div>';};
  const inner=(ico,n,d)=>'<div class="ico">'+ico+'</div><div class="n">'+n+'</div><div class="d">'+d+'</div>';
  const nav=(ico,n,d,view)=>'<div class="tile" style="cursor:pointer" onclick="go(\''+view+'\')">'+inner(ico,n,d)+'<span class="okdot"></span></div>';
  box.innerHTML=[
    nav("🖼","Image generation","ready · open Create","create"),
    tile("openwebui",3001,"💬","Chat (Open WebUI)","chat with any model"),
    (up("acestep")?tile("acestep",7860,"🎵","Music studio","ACE-Step generator")
      :'<div class="tile" style="cursor:pointer" onclick="startMusic()">'+inner("🎵","Music studio","off · click to start (~15 GB, ~1 min)")+'</div>'),
    tile("searxng",8888,"🔎","Search","private web search"),tile("portainer",9000,"🐳","Containers","Docker / Portainer")].join("");
}
function render(){
  const v=(location.hash||"#dashboard").slice(1);
  if(v==="dashboard"){renderKPIs();renderNow();renderLanes();renderKanban();}
  else if(v==="chat"){renderNow();}
  else if(v==="create")renderCreate();
  else if(v==="gallery")renderGallery();
  else if(v==="search")renderSearch();
  else if(v==="team")renderTeam();
  else if(v==="models")renderModels();
  else if(v==="data")renderData();
  else if(v==="system")renderSystem();
  else if(v==="usage")renderUsage();
  else if(v==="studio"){renderVoice();renderVideo();renderLaunchers();}
}
async function refresh(){
  try{state=await (await fetch("/api/state",{cache:"no-store"})).json();}catch(e){return;}
  const w=state.worker,m=state.machine||{},mem=state.mem||{};
  $("#np").className="pres"+(w&&w.state==="running"?" on":"");
  $("#nw").textContent=w?("worker "+w.state):"idle";
  $("#nm").innerHTML=(m.chip||"")+" · <b>"+(mem.free_gb||"?")+"</b>/"+(mem.total_gb||"?")+" GB";
  const sel=$("#c-persona");if(sel&&state.personas&&!sel.dataset.done){sel.innerHTML=state.personas.map(p=>'<option value="'+esc(p.name)+'">@'+esc(p.name)+'</option>').join("");if(state.personas.find(p=>p.name==="general"))sel.value="general";sel.dataset.done="1";}
  render();
}

/* drawer */
async function openJob(id){
  $("#scrim").classList.add("show");$("#drawer").classList.add("show");
  $("#dh").innerHTML='<div class="muted">loading #'+id+'…</div>';$("#db").innerHTML="";
  try{const j=await (await fetch("/api/job?id="+id,{cache:"no-store"})).json();const active=["running","queued","waiting"].includes(j.status);
    $("#dh").innerHTML='<span class="ava" style="width:34px;height:34px;font-size:16px">'+(AVA[j.persona]||"🤖")+'</span><div style="flex:1;min-width:0"><div style="font-weight:600;font-size:15px">#'+j.id+' · @'+esc(j.persona||j.model||"")+' <span class="badge b-'+j.status+'">'+j.status+'</span></div><div class="dim" style="font-size:12px;font-family:var(--mono)">'+esc(j.kind||"")+' · '+esc(j.model||"")+' · via '+esc(j.source||"")+(j.parent_id?" · sub of #"+j.parent_id:"")+'</div></div>'+(active?'<button class="btn ghost" onclick="cancelJob('+j.id+');closeJob()">cancel</button>':'')+'<a class="btn ghost" style="text-decoration:none" href="/api/job/export?id='+j.id+'" title="download this job as a Markdown report">⤓ .md</a><span class="x" onclick="closeJob()">✕</span>';
    const ic={think:"🧠",say:"💬",note:"·",tool:"🔧",result:"↳",delegate:"→",spec:"📝",model:"⚙",warn:"⚠",done:"✅",error:"❌",cancelled:"⏹"};
    const rows=(j.timeline||[]).map(e=>'<div class="ev '+e.k+'"><span class="ic">'+(ic[e.k]||"•")+'</span><span class="tx">'+(e.step?'<span class="dim">['+e.step+'] </span>':'')+esc(e.text||e.k)+'</span></div>').join("");
    const kids=((state&&state.jobs)||[]).filter(x=>x.parent_id===id);
    const sub=kids.length?'<div class="subtree"><div class="dim" style="font-size:10px;text-transform:uppercase;letter-spacing:.12em;margin-bottom:4px">Delegated sub-jobs</div>'+kids.map(k=>'<div class="s" onclick="openJob('+k.id+')">'+(AVA[k.persona]||"🤖")+' @'+esc(k.persona||"")+' #'+k.id+' <span class="badge b-'+k.status+'">'+k.status+'</span> '+esc((k.prompt||"").slice(0,46))+'</div>').join("")+'</div>':'';
    lastPlan=(j.plan&&j.plan.spec)?{task:j.plan.task||j.prompt,persona:j.persona,project:j.project||"",spec_text:j.plan.spec}:null;
    $("#db").innerHTML=(lastPlan?'<div class="report"><button class="btn" onclick="runPlan()">Run this plan ▶</button> <span class="dim">the spec below becomes the agent\'s brief</span></div>':'')+(j.prompt?'<div class="report"><b>Task:</b> '+esc(j.prompt)+'</div>':'')+(j.report?'<div class="report">'+esc(j.report)+'</div>':'')+(j.error?'<div class="report" style="color:var(--red)">'+esc(j.error)+'</div>':'')+sub+'<div class="tl">'+(rows||'<div class="muted">no activity yet</div>')+'</div>';
  }catch(e){$("#db").innerHTML='<div class="muted">could not load this job</div>';}
}
function closeJob(){$("#scrim").classList.remove("show");$("#drawer").classList.remove("show");}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeJob();});

/* actions */
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),2800);}
async function dispatch(){const task=$("#c-task").value.trim();if(!task){toast("type a task first");return;}
  const body={task,persona:$("#c-persona").value,project:$("#c-proj").value.trim(),plan_only:!!($("#c-plan")&&$("#c-plan").checked)};$("#c-run").disabled=true;
  try{const j=await (await fetch("/api/task",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json();
    if(j.ok){toast((body.plan_only?"planning job #":"dispatched job #")+j.id+" to @"+j.persona);$("#c-task").value="";refresh();}else toast("error: "+(j.error||"failed"));}
  catch(e){toast("network error");}finally{$("#c-run").disabled=false;}}
let lastPlan=null;
async function runPlan(){if(!lastPlan)return;
  try{const j=await (await fetch("/api/task",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(lastPlan)})).json();
    if(j.ok){toast("running the plan as job #"+j.id);closeJob();refresh();}else toast("error: "+(j.error||"failed"));}catch(e){toast("network error");}}
async function startMusic(){try{const j=await (await fetch("/api/music",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json();toast(j.note||"starting…");}catch(e){toast("could not start ACE-Step");}}
async function unloadModel(){try{const j=await (await fetch("/api/unload",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json();toast(j.note||(j.ok?"unloading the model…":(j.error||"failed")));setTimeout(()=>{refresh();if(typeof renderSystem==="function")renderSystem();},2500);}catch(e){toast("could not unload");}}
async function cancelJob(id){try{await fetch("/api/cancel",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})});toast("cancelled #"+id);refresh();}catch(e){toast("could not cancel");}}

const IMG_MODELS=[["z-image-turbo","Z-Image Turbo · fast, best balance"],["qwen","Qwen-Image · top quality, slow"],["flux2","FLUX.2 klein · fast"],["z-image","Z-Image · photoreal"],["fibo","FIBO"]];
function fillImgModels(){const s=$("#img-model");if(!s||s.dataset.done)return;s.innerHTML=IMG_MODELS.map(m=>'<option value="'+m[0]+'">'+m[1]+'</option>').join("");s.dataset.done="1";}
let imgPoll=null;
async function generateImage(){
  const prompt=$("#img-prompt").value.trim();if(!prompt){toast("describe the image first");return;}
  const sz=parseInt($("#img-size").value)||1024;
  const body={prompt,base_model:$("#img-model").value,width:sz,height:sz};
  $("#img-run").disabled=true;
  $("#img-out").innerHTML='<div class="idle">⏳ queued — the model loads on first use (first run downloads weights, can take a few minutes)…</div>';
  try{
    const j=await (await fetch("/api/image",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json();
    if(!j.ok){$("#img-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(j.error||"could not start")+'</div>';$("#img-run").disabled=false;return;}
    const id=j.id;
    if(imgPoll)clearInterval(imgPoll);
    imgPoll=setInterval(async()=>{
      try{
        const d=await (await fetch("/api/job?id="+id,{cache:"no-store"})).json();
        const note=(d.timeline||[]).slice().reverse().find(e=>e.k==="model"||e.k==="note"||e.k==="warn");
        if(d.status==="done"){
          clearInterval(imgPoll);$("#img-run").disabled=false;
          const m=(d.report||"").match(/img-\d+\.png|[\w.-]+\.png/);
          const name=m?m[0].split("/").pop():"";
          $("#img-out").innerHTML=name?'<img src="/media?path='+encodeURIComponent(name)+'" style="max-width:100%;border-radius:12px;border:1px solid var(--bdr)"><div class="dim" style="font-size:12px;margin-top:6px;font-family:var(--mono)">'+esc(d.report||"")+'</div>':'<div class="report">'+esc(d.report||"done")+'</div>';
        }else if(d.status==="failed"){clearInterval(imgPoll);$("#img-run").disabled=false;$("#img-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(d.error||"generation failed")+'</div>';}
        else{$("#img-out").innerHTML='<div class="idle">🎨 generating (#'+id+')… '+esc(note?(note.text||""):d.status)+'</div>';}
      }catch(e){}
    },2500);
  }catch(e){$("#img-out").innerHTML='<div class="report" style="color:var(--red)">network error</div>';$("#img-run").disabled=false;}
}

/* CREATE page */
const CR_MODELS=[["z-image-turbo","Z-Image Turbo","fast · Apache-2.0 · best balance"],["qwen","Qwen-Image 20B","top quality · slow ~6min"],["flux2","FLUX.2 klein","fast · Apache-2.0"],["fibo","FIBO","alternative"]];
const CR_STYLES=[["Photorealistic",", photorealistic, DSLR, 4k sharp"],["Cinematic",", cinematic film still, anamorphic, color graded"],["Anime",", anime illustration, cel shaded, studio quality"],["Oil Painting",", oil painting, impasto, museum quality"],["Digital Art",", digital concept art, artstation quality"],["Watercolor",", watercolor painting, soft edges"],["3D Render",", 3D render, octane render, hyperrealistic"],["Sketch",", pencil sketch, fine line art"]];
const CR_RES=[["Square",1024,1024],["Portrait",1024,1344],["Landscape",1344,1024],["Wide",1536,1024]];
let crModel="z-image-turbo", crRes=[1024,1024], crPoll=null;
function renderCreate(){
  const mroot=$("#cr-models"); if(!mroot||mroot.dataset.done) return;
  mroot.innerHTML=CR_MODELS.map(m=>'<button class="opt'+(m[0]===crModel?' on':'')+'">'+m[1]+'</button>').join("");
  mroot.querySelectorAll("button").forEach((b,i)=>b.onclick=()=>{crModel=CR_MODELS[i][0];mroot.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));$("#cr-modelnote").textContent=CR_MODELS[i][2];});
  $("#cr-modelnote").textContent=CR_MODELS[0][2];
  $("#cr-styles").innerHTML=CR_STYLES.map(s=>'<button class="opt">'+s[0]+'</button>').join("");
  $("#cr-styles").querySelectorAll("button").forEach(b=>b.onclick=()=>b.classList.toggle("on"));
  $("#cr-res").innerHTML=CR_RES.map((r,i)=>'<button class="opt'+(i===0?' on':'')+'">'+r[0]+' · '+r[1]+'×'+r[2]+'</button>').join("");
  $("#cr-res").querySelectorAll("button").forEach((b,i)=>b.onclick=()=>{crRes=[CR_RES[i][1],CR_RES[i][2]];$("#cr-res").querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));});
  mroot.dataset.done="1";
}
async function generateCreate(){
  let prompt=$("#cr-prompt").value.trim(); if(!prompt){toast("describe the image first");return;}
  $("#cr-styles").querySelectorAll(".opt.on").forEach(b=>{const s=CR_STYLES.find(x=>x[0]===b.textContent);if(s)prompt+=s[1];});
  const body={prompt,base_model:crModel,width:crRes[0],height:crRes[1],steps:+$("#cr-steps").value,negative_prompt:$("#cr-neg").value.trim()};
  const seed=$("#cr-seed").value.trim(); if(/^\d+$/.test(seed))body.seed=+seed;
  $("#cr-run").disabled=true; $("#cr-out").innerHTML='<div class="idle">⏳ queued — generating (first run downloads the model)…</div>';
  try{
    const j=await (await fetch("/api/image",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json();
    if(!j.ok){$("#cr-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(j.error||"failed")+'</div>';$("#cr-run").disabled=false;return;}
    const id=j.id; if(crPoll)clearInterval(crPoll);
    crPoll=setInterval(async()=>{try{
      const d=await (await fetch("/api/job?id="+id,{cache:"no-store"})).json();
      if(d.status==="done"){clearInterval(crPoll);$("#cr-run").disabled=false;
        const m=(d.report||"").match(/[\w.-]+\.png/);const name=m?m[0]:"";
        if(name){const url="/media?path="+encodeURIComponent(name);
          $("#cr-out").innerHTML='<img src="'+url+'" style="max-width:100%;border-radius:12px;border:1px solid var(--bdr)"><div class="dim" style="font-size:12px;margin-top:6px;font-family:var(--mono)">'+esc(d.report||"")+'</div>';
          $("#cr-gallery").insertAdjacentHTML("afterbegin",'<img src="'+url+'" onclick="window.open(this.src)">');
        } else $("#cr-out").innerHTML='<div class="report">'+esc(d.report||"done")+'</div>';
      } else if(d.status==="failed"){clearInterval(crPoll);$("#cr-run").disabled=false;$("#cr-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(d.error||"generation failed")+'</div>';}
      else{const n=(d.timeline||[]).slice().reverse().find(e=>e.k==="model"||e.k==="note"||e.k==="warn");$("#cr-out").innerHTML='<div class="idle">🎨 generating (#'+id+')… '+esc(n?(n.text||""):d.status)+'</div>';}
    }catch(e){}},2500);
  }catch(e){$("#cr-out").innerHTML='<div class="report" style="color:var(--red)">network error</div>';$("#cr-run").disabled=false;}
}
/* DATA page (db viewer) */
let dbActive="queue";
async function renderData(){
  const root=$("#db-tables"); if(!root) return;
  root.innerHTML='<div class="idle">loading…</div>';
  let s; try{s=await (await fetch("/api/db",{cache:"no-store"})).json();}catch(e){root.innerHTML='<div class="idle">could not load</div>';return;}
  root.innerHTML=(s.databases||[]).map(db=>'<div class="dbname">'+esc(db.name)+'.db'+(db.exists?'':' (missing)')+'</div><div class="dbtbl">'+
    (db.tables||[]).map(t=>'<div class="tb" onclick="pickTable(\''+esc(db.name)+'\',\''+esc(t.name)+'\')"><span>'+esc(t.name)+'</span><span class="rows">'+(t.rows==null?'?':t.rows)+' rows</span></div>').join("")+'</div>').join("");
}
function pickTable(db,t){dbActive=db;$("#db-active").textContent="· "+db+".db";$("#db-sql").value="SELECT * FROM "+t+" ORDER BY 1 DESC LIMIT 50";runQuery();}
async function runQuery(){
  const sql=$("#db-sql").value.trim(); if(!sql)return;
  $("#db-info").textContent="running…"; $("#db-active").textContent="· "+dbActive+".db";
  try{const d=await (await fetch("/api/db/query",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({db:dbActive,sql})})).json();
    if(d.error){$("#db-out").innerHTML="";$("#db-info").innerHTML='<span style="color:var(--red)">'+esc(d.error)+'</span>';return;}
    $("#db-info").textContent=(d.rows||[]).length+" rows"+(d.truncated?" (capped 1000)":"");
    const th="<tr>"+(d.columns||[]).map(c=>"<th>"+esc(c)+"</th>").join("")+"</tr>";
    const tr=(d.rows||[]).map(r=>"<tr>"+r.map(v=>'<td title="'+esc(v==null?"":String(v))+'">'+esc(v==null?"∅":String(v).slice(0,300))+"</td>").join("")+"</tr>").join("");
    $("#db-out").innerHTML='<table class="dtable">'+th+tr+"</table>";
  }catch(e){$("#db-info").textContent="error";}
}

/* VOICE / TTS */
const TTS_VOICES=[["af_sky","Sky · F warm"],["af_heart","Heart · F soft"],["am_adam","Adam · M deep"],["am_echo","Echo · M resonant"],["bf_emma","Emma · British F"],["bm_lewis","Lewis · British M"]];
let ttsVoice="af_sky", ttsPoll=null;
function renderVoice(){
  const r=$("#tts-voices"); if(!r||r.dataset.done)return;
  r.innerHTML=TTS_VOICES.map(v=>'<button class="opt'+(v[0]===ttsVoice?' on':'')+'">'+v[1]+'</button>').join("");
  r.querySelectorAll("button").forEach((b,i)=>b.onclick=()=>{ttsVoice=TTS_VOICES[i][0];r.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));});
  r.dataset.done="1";
}
async function generateTTS(){
  const text=$("#tts-text").value.trim(); if(!text){toast("type something first");return;}
  $("#tts-run").disabled=true; $("#tts-out").innerHTML='<div class="idle">⏳ synthesizing…</div>';
  try{
    const j=await (await fetch("/api/tts",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text,voice:ttsVoice})})).json();
    if(!j.ok){$("#tts-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(j.error||"failed")+'</div>';$("#tts-run").disabled=false;return;}
    const id=j.id; if(ttsPoll)clearInterval(ttsPoll);
    ttsPoll=setInterval(async()=>{try{
      const d=await (await fetch("/api/job?id="+id,{cache:"no-store"})).json();
      if(d.status==="done"){clearInterval(ttsPoll);$("#tts-run").disabled=false;
        const m=(d.report||"").match(/[\w.-]+\.wav/);const name=m?m[0]:"";
        $("#tts-out").innerHTML=name?'<audio controls autoplay src="/media?path='+encodeURIComponent(name)+'" style="width:100%"></audio>':'<div class="report">'+esc(d.report||"done")+'</div>';
      }else if(d.status==="failed"){clearInterval(ttsPoll);$("#tts-run").disabled=false;$("#tts-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(d.error||"failed")+'</div>';}
    }catch(e){}},2000);
  }catch(e){$("#tts-out").innerHTML='<div class="report" style="color:var(--red)">network error</div>';$("#tts-run").disabled=false;}
}

/* SEARCH (RAG) */
async function renderSearch(){
  const el=$("#rag-indexed"); if(!el)return;
  try{const s=await (await fetch("/api/rag/status",{cache:"no-store"})).json();
    el.innerHTML=(s.indexed||[]).length?("indexed: "+s.indexed.map(x=>esc(x.root.split("/").pop())+" ("+x.files+"f/"+x.chunks+"c)").join(", ")):"nothing indexed yet";
    if((s.indexed||[]).length&&!$("#rag-root").value)$("#rag-root").value=s.indexed[0].root;
  }catch(e){}
}
async function indexFolder(){
  const root=$("#rag-root").value.trim(); if(!root){toast("enter a folder");return;}
  $("#rag-index").disabled=true; $("#rag-out").innerHTML='<div class="idle">📇 indexing (loads the embedding model)…</div>';
  try{const j=await (await fetch("/api/rag/index",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({root})})).json();
    if(!j.ok){$("#rag-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(j.error||"failed")+'</div>';$("#rag-index").disabled=false;return;}
    const id=j.id, poll=setInterval(async()=>{try{
      const d=await (await fetch("/api/job?id="+id,{cache:"no-store"})).json();
      if(d.status==="done"){clearInterval(poll);$("#rag-index").disabled=false;$("#rag-out").innerHTML='<div class="report">'+esc(d.report||"indexed")+'</div>';renderSearch();}
      else if(d.status==="failed"){clearInterval(poll);$("#rag-index").disabled=false;$("#rag-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(d.error||"failed")+'</div>';}
      else{const n=(d.timeline||[]).slice().reverse().find(e=>e.k==="index"||e.k==="note");$("#rag-out").innerHTML='<div class="idle">📇 indexing… '+esc(n?(n.text||""):"")+'</div>';}
    }catch(e){}},2500);
  }catch(e){$("#rag-index").disabled=false;}
}
async function runSearch(){
  const root=$("#rag-root").value.trim(), query=$("#rag-q").value.trim();
  if(!root){toast("enter a folder");return;} if(!query){toast("enter a query");return;}
  $("#rag-run").disabled=true; $("#rag-out").innerHTML='<div class="idle">🔎 searching…</div>';
  try{const j=await (await fetch("/api/rag/search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({root,query})})).json();
    if(!j.ok){$("#rag-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(j.error||"failed")+'</div>';$("#rag-run").disabled=false;return;}
    const id=j.id, poll=setInterval(async()=>{try{
      const d=await (await fetch("/api/rag/result?id="+id,{cache:"no-store"})).json();
      if(d.status==="done"){clearInterval(poll);$("#rag-run").disabled=false;
        const rs=d.results||[];
        $("#rag-out").innerHTML=rs.length?rs.map(r=>'<div class="report"><div class="dim" style="font-size:11.5px;font-family:var(--mono)">'+esc(r.path)+' #'+r.chunk+' · score '+r.score+'</div><div style="margin-top:5px;font-size:12.5px;white-space:pre-wrap">'+esc(r.text)+'</div></div>').join(""):'<div class="idle">no matches — index the folder first?</div>';
      }else if(d.status==="failed"){clearInterval(poll);$("#rag-run").disabled=false;$("#rag-out").innerHTML='<div class="report" style="color:var(--red)">search failed (is the folder indexed?)</div>';}
    }catch(e){}},2000);
  }catch(e){$("#rag-run").disabled=false;}
}

/* SYSTEM view */
async function renderGallery(){
  const g=$("#gallery-grid"); if(!g) return;
  let u; try{u=await (await fetch("/api/gallery",{cache:"no-store"})).json();}catch(e){g.innerHTML='<div class="idle">could not load</div>';return;}
  const items=u.items||[];
  if(!items.length){g.innerHTML='<div class="idle">nothing generated yet — try the Create or Studio tabs</div>';return;}
  const media=it=>{const src="/media?path="+encodeURIComponent(it.name);
    if(it.type==="image")return '<img src="'+src+'" loading="lazy" style="width:100%;height:170px;object-fit:cover;border-radius:10px 10px 0 0;display:block">';
    if(it.type==="video")return '<video src="'+src+'" controls preload="metadata" style="width:100%;height:170px;object-fit:cover;border-radius:10px 10px 0 0;background:#000"></video>';
    return '<div style="height:170px;display:flex;align-items:center;justify-content:center;background:var(--panel);border-radius:10px 10px 0 0;font-size:34px">🔊</div><audio src="'+src+'" controls style="width:100%"></audio>';};
  g.innerHTML=items.map(it=>'<div class="card" style="padding:0;overflow:hidden">'+media(it)+'<div style="padding:8px 10px"><div style="font-family:var(--mono);font-size:11px;color:var(--muted)">'+esc(it.type)+' · '+new Date(it.ts*1000).toLocaleString()+'</div></div></div>').join("");
}
function fmtDur(s){s=Math.round(s||0);if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"m "+(s%60)+"s";return Math.floor(s/3600)+"h "+Math.floor((s%3600)/60)+"m";}
async function renderUsage(){
  const k=$("#usage-kpis"); if(!k) return;
  let u; try{u=await (await fetch("/api/usage",{cache:"no-store"})).json();}catch(e){k.innerHTML='<div class="idle">could not load</div>';return;}
  const st=u.by_status||{}, failed=(st.failed||0)+(st.cancelled||0);
  k.innerHTML=[["TOTAL JOBS",u.total||0],["COMPUTE TIME",fmtDur(u.compute_s)],["DONE",st.done||0],["FAILED / CANCELLED",failed]]
    .map(x=>'<div class="kpi"><div class="l">'+x[0]+'</div><div class="v">'+x[1]+'</div></div>').join("");
  const maxDay=Math.max(1,...(u.by_day||[]).map(d=>d.jobs));
  $("#usage-days").innerHTML=(u.by_day||[]).length?(u.by_day).map(d=>'<div class="svc"><div class="dim" style="width:94px;font-family:var(--mono);font-size:11.5px">'+esc(d.day)+'</div><div style="flex:1;background:var(--line);border-radius:6px;height:14px;overflow:hidden"><div style="height:100%;width:'+Math.round(d.jobs/maxDay*100)+'%;background:var(--blue)"></div></div><div style="width:36px;text-align:right">'+d.jobs+'</div></div>').join(""):'<div class="idle">no jobs yet</div>';
  const tbl=(rows,cols)=>'<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>'+cols.map(c=>'<th style="text-align:left;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--line)">'+c+'</th>').join("")+'</tr></thead><tbody>'+rows+'</tbody></table>';
  const td='style="padding:6px 8px;border-bottom:1px solid var(--line)"';
  $("#usage-kind").innerHTML=(u.by_kind||[]).length?tbl((u.by_kind).map(r=>'<tr><td '+td+'>'+esc(r.kind)+'</td><td '+td+'>'+r.jobs+'</td><td '+td+'>'+fmtDur(r.secs)+'</td><td '+td+'>'+fmtDur(r.avg)+'</td></tr>').join(""),["Kind","Jobs","Compute","Avg"]):'<div class="idle">—</div>';
  const mh=u.model_health||{};
  $("#usage-model").innerHTML=(u.by_model||[]).length?tbl((u.by_model).map(r=>{const h=mh[r.model]||{};return '<tr><td '+td+' style="font-family:var(--mono);padding:6px 8px">'+esc(r.model)+'</td><td '+td+'>'+r.jobs+'</td><td '+td+'>'+fmtDur(r.secs)+'</td><td '+td+'>'+(h.loads?h.loads+' × ~'+fmtDur(h.avg_load_s):'—')+'</td><td '+td+' title="'+esc(h.last_error||"")+'">'+(h.failed_jobs?'<span style="color:var(--red)">'+h.failed_jobs+'</span>':'0')+'</td></tr>';}).join(""),["Model","Jobs","Compute","Loads","Failed"]):'<div class="idle">—</div>';
}
async function renderSystem(){
  const nn=$("#sys-native"), dd=$("#sys-docker"); if(!nn) return;
  let s; try{s=await (await fetch("/api/system",{cache:"no-store"})).json();}catch(e){nn.innerHTML='<div class="idle">could not load</div>';return;}
  const row=x=>'<div class="svc"><span class="svc-dot" style="background:'+(x.up?"var(--green)":"var(--dim)")+'"></span><div style="flex:1;min-width:0"><div class="svc-nm">'+esc(x.name)+'</div><div class="dim" style="font-size:11.5px;font-family:var(--mono)">'+esc(x.detail||x.status||x.ports||"")+'</div></div><span class="badge '+(x.up?"b-done":"b-queued")+'">'+(x.up?"up":"down")+'</span></div>';
  nn.innerHTML=(s.native||[]).map(row).join("");
  dd.innerHTML=(s.docker||[]).length?(s.docker||[]).map(x=>row({name:x.name,up:x.up,detail:(x.ports||"")+" · "+(x.status||"")})).join(""):'<div class="idle">no containers</div>';
  const dk=$("#sys-disk"), d=s.disk||{};
  if(dk){
    if(d.error){dk.innerHTML='<div class="idle">'+esc(d.error)+'</div>';}
    else{
      const col=d.level==="critical"?"var(--red)":d.level==="low"?"var(--amber,#d97706)":"var(--green)";
      const pct=d.total_gb?Math.round(100*(d.total_gb-d.free_gb)/d.total_gb):0;
      dk.innerHTML='<div class="svc"><span class="svc-dot" style="background:'+col+'"></span><div style="flex:1"><div class="svc-nm">'+d.free_gb+' GB free of '+d.total_gb+' GB</div>'
        +'<div style="height:6px;border-radius:3px;background:var(--line,#333);margin-top:6px"><div style="height:6px;border-radius:3px;width:'+pct+'%;background:'+col+'"></div></div></div></div>'
        +(d.items||[]).filter(x=>x.gb>=0.1).sort((a,b)=>b.gb-a.gb).map(x=>'<div class="svc"><div style="flex:1;min-width:0"><div class="svc-nm">'+esc(x.name)+'</div><div class="dim" style="font-size:11.5px;font-family:var(--mono)">'+esc(x.note||x.path)+'</div></div><span class="badge b-queued">'+x.gb+' GB</span></div>').join("");
    }
  }
}

/* VIDEO / LTX */
let vidDur=5, vidPoll=null;
function renderVideo(){
  const r=$("#vid-dur"); if(!r||r.dataset.done)return;
  r.innerHTML=[3,5,8].map(d=>'<button class="opt'+(d===vidDur?' on':'')+'">'+d+'s</button>').join("");
  r.querySelectorAll("button").forEach((b,i)=>b.onclick=()=>{vidDur=[3,5,8][i];r.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));});
  r.dataset.done="1";
}
async function generateVideo(){
  const prompt=$("#vid-prompt").value.trim(); if(!prompt){toast("describe the video first");return;}
  $("#vid-run").disabled=true; $("#vid-out").innerHTML='<div class="idle">⏳ queued — first run downloads ~21GB, then generates…</div>';
  try{
    const j=await (await fetch("/api/video",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt,duration_s:vidDur})})).json();
    if(!j.ok){$("#vid-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(j.error||"failed")+'</div>';$("#vid-run").disabled=false;return;}
    const id=j.id; if(vidPoll)clearInterval(vidPoll);
    vidPoll=setInterval(async()=>{try{
      const d=await (await fetch("/api/job?id="+id,{cache:"no-store"})).json();
      if(d.status==="done"){clearInterval(vidPoll);$("#vid-run").disabled=false;
        const m=(d.report||"").match(/[\w.-]+\.mp4/);const name=m?m[0]:"";
        $("#vid-out").innerHTML=name?'<video controls autoplay loop src="/media?path='+encodeURIComponent(name)+'" style="width:100%;border-radius:12px"></video>':'<div class="report">'+esc(d.report||"done")+'</div>';
      }else if(d.status==="failed"){clearInterval(vidPoll);$("#vid-run").disabled=false;$("#vid-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(d.error||"failed")+'</div>';}
      else{const n=(d.timeline||[]).slice().reverse().find(e=>e.k==="video"||e.k==="note");$("#vid-out").innerHTML='<div class="idle">🎬 working (#'+id+')… '+esc(n?(n.text||n.note||""):d.status)+'</div>';}
    }catch(e){}},4000);
  }catch(e){$("#vid-out").innerHTML='<div class="report" style="color:var(--red)">network error</div>';$("#vid-run").disabled=false;}
}

let visPoll=null;
async function analyseImage(){
  const f=$("#vis-file").files[0]; if(!f){toast("choose an image first");return;}
  if(f.size>12000000){toast("image too large (max 12MB)");return;}
  const prompt=$("#vis-prompt").value.trim();
  const ext=(f.name.split(".").pop()||"png").toLowerCase();
  $("#vis-run").disabled=true; $("#vis-out").innerHTML='<div class="idle">⏳ reading image…</div>';
  const reader=new FileReader();
  reader.onload=async()=>{
    const thumb='<img src="'+reader.result+'" style="max-width:240px;border-radius:12px;margin-bottom:10px;display:block">';
    try{
      const j=await (await fetch("/api/vision",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({image_b64:reader.result,ext,prompt})})).json();
      if(!j.ok){$("#vis-out").innerHTML='<div class="report" style="color:var(--red)">'+esc(j.error||"failed")+'</div>';$("#vis-run").disabled=false;return;}
      const id=j.id; $("#vis-out").innerHTML=thumb+'<div class="idle">👁 analysing (#'+id+')…</div>';
      if(visPoll)clearInterval(visPoll);
      visPoll=setInterval(async()=>{try{
        const d=await (await fetch("/api/job?id="+id,{cache:"no-store"})).json();
        if(d.status==="done"){clearInterval(visPoll);$("#vis-run").disabled=false;$("#vis-out").innerHTML=thumb+'<div class="report">'+esc(d.report||"(no description)")+'</div>';}
        else if(d.status==="failed"){clearInterval(visPoll);$("#vis-run").disabled=false;$("#vis-out").innerHTML=thumb+'<div class="report" style="color:var(--red)">'+esc(d.error||"failed")+'</div>';}
        else{$("#vis-out").innerHTML=thumb+'<div class="idle">👁 analysing (#'+id+')… '+esc(d.status)+'</div>';}
      }catch(e){}},2500);
    }catch(e){$("#vis-out").innerHTML='<div class="report" style="color:var(--red)">network error</div>';$("#vis-run").disabled=false;}
  };
  reader.readAsDataURL(f);
}

/* boot */
window.addEventListener("load",()=>setTimeout(()=>$("#splash").classList.add("gone"),1400));
document.addEventListener("DOMContentLoaded",()=>{
  const t=$("#c-task");if(t){$("#c-run").addEventListener("click",dispatch);t.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();dispatch();}});}
  const ir=$("#img-run");if(ir)ir.addEventListener("click",generateImage);
  const cr=$("#cr-run");if(cr)cr.addEventListener("click",generateCreate);
  const dr=$("#db-run");if(dr)dr.addEventListener("click",runQuery);
  const tr=$("#tts-run");if(tr)tr.addEventListener("click",generateTTS);
  const vr=$("#vid-run");if(vr)vr.addEventListener("click",generateVideo);
  const vz=$("#vis-run");if(vz)vz.addEventListener("click",analyseImage);
  const rr=$("#rag-run");if(rr)rr.addEventListener("click",runSearch);
  const ri=$("#rag-index");if(ri)ri.addEventListener("click",indexFolder);
  const rq=$("#rag-q");if(rq)rq.addEventListener("keydown",e=>{if(e.key==="Enter")runSearch();});
  route();poll();refresh();
  setInterval(poll,550);setInterval(()=>{renderNow();},550);setInterval(refresh,2200);
});
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
