#!/usr/bin/env python3
"""MLX AI Workstation — control dashboard: live status, persona management, docs."""
import os, socket, datetime, secrets, hmac, re, sys, json, importlib.util, uuid as _uuid
import threading as _th, time as _time
import queue as _queue
from collections import deque
import requests, psutil
from functools import wraps
from pathlib import Path
from flask import Flask, jsonify, Response, request, session, redirect

P_MLX="8000"; P_GATEWAY="4000"; P_DASH="8800"
P_OWUI="3001"; P_SEARX="8888"; P_LF="3000"; P_PORTAINER="9000"

WORKDIR = Path(os.environ.get("MLX_WORKDIR", str(Path.home() / ".mlx-ai-workstation")))
ENV_FILE = WORKDIR / ".env"
AGENT_PATH = WORKDIR / "agent" / "mlx-agent.py"

def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip()
    return env
ENV = load_env()

# Reuse the TESTED agent data layer (personas + tool registry) — the UI never
# reimplements persona logic, it calls load_personas/save_personas/ALL_SCHEMAS.
try:
    _spec = importlib.util.spec_from_file_location("mlx_agent", str(AGENT_PATH))
    agent = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(agent)
except Exception as e:
    agent = None; print("dashboard: agent module not loaded yet:", e)

app = Flask(__name__)
app.secret_key = ENV.get("DASHBOARD_SECRET") or secrets.token_hex(16)

# Auth is enabled only when the dashboard is exposed beyond localhost (user's choice).
DASH_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
_LOOPBACK = DASH_HOST in ("127.0.0.1", "localhost", "::1", "")
DASH_PASSWORD = ENV.get("DASHBOARD_PASSWORD", "")
if not _LOOPBACK and not DASH_PASSWORD:
    DASH_PASSWORD = secrets.token_urlsafe(10)
    print(f"dashboard: exposed on {DASH_HOST} with no DASHBOARD_PASSWORD set — "
          f"generated a temporary one: {DASH_PASSWORD}  (set a permanent one via ./mlx-setup.sh --configure)")
AUTH_ON = False

def require_auth(f):
    @wraps(f)
    def w(*a, **k):
        if AUTH_ON and not session.get("ok"):
            if request.path.startswith("/api/"): return jsonify({"error": "auth required"}), 401
            return redirect("/login")
        return f(*a, **k)
    return w

SERVICES = [
    ("Inference (MLX)",          f"http://127.0.0.1:{P_MLX}/v1/models",           P_MLX,     "Local models on Metal"),
    ("Gateway (LiteLLM)",        f"http://127.0.0.1:{P_GATEWAY}/health/liveliness", P_GATEWAY, "OpenAI-compatible router"),
    ("Web chat (Open WebUI)",    f"http://127.0.0.1:{P_OWUI}/",                   P_OWUI,    "Pick a model & chat"),
    ("Private search (SearXNG)", f"http://127.0.0.1:{P_SEARX}/",                  P_SEARX,   "Web results for RAG"),
    ("Tracing (Langfuse)",       f"http://127.0.0.1:{P_LF}/api/public/health",    P_LF,      "Optional observability"),
    ("Docker (Portainer)",       f"http://127.0.0.1:{P_PORTAINER}/api/status",    P_PORTAINER, "Docker image & container manager"),
    ("Music Studio (ACE-Step)",  "http://127.0.0.1:7860/",                        "7860",    "Text-to-music generation"),
    ("Dashboard",                f"http://127.0.0.1:{P_DASH}/",                   P_DASH,    "This page"),
]

# launchctl label or docker container name for each service restart
_SVC_RESTART = {
    "Inference (MLX)":          {"kind": "launchd",  "id": "com.mlxstack.inference"},
    "Gateway (LiteLLM)":        {"kind": "launchd",  "id": "com.mlxstack.gateway"},
    "Web chat (Open WebUI)":    {"kind": "docker",   "id": "open-webui"},
    "Private search (SearXNG)": {"kind": "docker",   "id": "searxng"},
    "Tracing (Langfuse)":       {"kind": "launchd",  "id": "com.mlxstack.langfuse"},
    "Docker (Portainer)":       {"kind": "docker",   "id": "portainer"},
    "Music Studio (ACE-Step)":  {"kind": "launchd",  "id": "com.mlxstack.acestep"},
    "Dashboard":                {"kind": "launchd",  "id": "com.mlxstack.dashboard"},
}

# Role → served model, for the demo model table.
ROLES = [
    ("Orchestrator", "qwen36-35b",   "Reads the task, routes work to the right sub-model."),
    ("Coder",        "qwen36-27b",   "Writes & edits code (dense 27B, 8-bit for accuracy)."),
    ("Reasoner / QA","qwen36-27b",   "Plans, reviews, designs tests (thinking mode)."),
    ("Vision / OCR", "qwen36-27b",  "Reads images, PDFs, charts, screenshots (multimodal 27B)."),
    ("Embeddings",   "qwen3-embed",  "Turns documents into vectors for RAG."),
]

def probe(url):
    try: return requests.get(url, timeout=3).status_code < 500
    except Exception: return False

def loaded_models():
    try:
        r = requests.get(f"http://127.0.0.1:{P_MLX}/v1/models", timeout=3)
        return {m.get("id","") for m in r.json().get("data", [])}
    except Exception:
        return set()

def hardware():
    vm = psutil.virtual_memory()
    try: disk = psutil.disk_usage(os.path.expanduser("~"))
    except Exception: disk = None
    bat = {}
    try:
        b = psutil.sensors_battery()
        if b:
            secs = b.secsleft
            hrs = f"{int(secs//3600)}h{int((secs%3600)//60)}m" if secs and secs > 0 else ("charging" if b.power_plugged else "—")
            bat = {"pct": round(b.percent), "charging": b.power_plugged, "time_str": hrs}
    except Exception: pass
    return {
        "cpu": round(psutil.cpu_percent(interval=None)),
        "ram_pct": round(vm.percent), "ram_txt": f"{vm.used/1e9:.0f} / {vm.total/1e9:.0f} GB",
        "disk_pct": round(disk.percent) if disk else 0,
        "disk_txt": f"{disk.used/1e9:.0f} / {disk.total/1e9:.0f} GB" if disk else "?",
        "battery": bat,
    }

# ── rolling 5-minute metric history (Task-Manager-style graphs) ─────────────
_METRICS = deque(maxlen=100)
_SAMPLER_STARTED = False
_IO_BASE = {}

def _sample_loop():
    global _IO_BASE
    # seed baseline for rate calculation
    try:
        di = psutil.disk_io_counters(); ni = psutil.net_io_counters()
        _IO_BASE = {"ts": _time.time(), "dr": di.read_bytes, "dw": di.write_bytes,
                    "nr": ni.bytes_recv, "ns": ni.bytes_sent}
    except Exception:
        _IO_BASE = {"ts": _time.time(), "dr": 0, "dw": 0, "nr": 0, "ns": 0}
    while True:
        try:
            vm = psutil.virtual_memory()
            cpu = round(psutil.cpu_percent(interval=None))
            now = _time.time()
            dt = max(now - _IO_BASE.get("ts", now - 3), 0.001)
            disk_r = disk_w = net_r = net_s = 0.0
            try:
                di = psutil.disk_io_counters(); ni = psutil.net_io_counters()
                disk_r = max(0, (di.read_bytes - _IO_BASE["dr"]) / dt / 1e6)
                disk_w = max(0, (di.write_bytes - _IO_BASE["dw"]) / dt / 1e6)
                net_r = max(0, (ni.bytes_recv - _IO_BASE["nr"]) / dt / 1e6)
                net_s = max(0, (ni.bytes_sent - _IO_BASE["ns"]) / dt / 1e6)
                _IO_BASE.update({"ts": now, "dr": di.read_bytes, "dw": di.write_bytes,
                                 "nr": ni.bytes_recv, "ns": ni.bytes_sent})
            except Exception:
                _IO_BASE["ts"] = now
            bat_pct = -1
            try:
                b = psutil.sensors_battery()
                if b: bat_pct = round(b.percent)
            except Exception: pass
            _METRICS.append({"t": int(now), "cpu": cpu,
                             "ram": round(vm.percent), "ram_gb": round(vm.used / 1e9, 1),
                             "dr": round(disk_r, 2), "dw": round(disk_w, 2),
                             "nr": round(net_r, 2), "ns": round(net_s, 2),
                             "bat": bat_pct})
        except Exception: pass
        _time.sleep(3)

def _start_sampler():
    global _SAMPLER_STARTED
    if not _SAMPLER_STARTED:
        _SAMPLER_STARTED = True
        _th.Thread(target=_sample_loop, daemon=True).start()
_start_sampler()

@app.route("/api/metrics")
@require_auth
def api_metrics():
    data = list(_METRICS)
    vm = psutil.virtual_memory()
    bat = {"pct": -1, "charging": False, "time_str": "—"}
    try:
        b = psutil.sensors_battery()
        if b:
            secs = b.secsleft
            hrs = f"{int(secs//3600)}h{int((secs%3600)//60)}m" if secs and secs > 0 else ("charging" if b.power_plugged else "—")
            bat = {"pct": round(b.percent), "charging": b.power_plugged, "time_str": hrs}
    except Exception: pass
    def _col(key): return [d.get(key, 0) for d in data]
    try:
        _disk = psutil.disk_usage(os.path.expanduser("~"))
        disk_pct = round(_disk.percent)
        disk_used_gb = round(_disk.used / 1e9)
        disk_total_gb = round(_disk.total / 1e9)
    except Exception:
        disk_pct, disk_used_gb, disk_total_gb = 0, 0, 0
    return jsonify({
        "cpu": _col("cpu"), "ram": _col("ram"), "ram_gb": _col("ram_gb"),
        "disk_r": _col("dr"), "disk_w": _col("dw"),
        "net_r": _col("nr"), "net_s": _col("ns"),
        "bat": _col("bat"),
        "ram_total_gb": round(vm.total / 1e9),
        "disk_pct": disk_pct, "disk_used_gb": disk_used_gb, "disk_total_gb": disk_total_gb,
        "battery": bat, "window_sec": 300,
    })

ACTIVITY_DIR = WORKDIR / "activity"
SSE_EVENTS_FILE = WORKDIR / "activity" / "events.jsonl"

@app.route("/api/clear-cache", methods=["POST"])
@require_auth
def api_clear_cache():
    _METRICS.clear()
    try:
        if SSE_EVENTS_FILE.exists():
            SSE_EVENTS_FILE.write_text("")
    except Exception:
        pass
    return jsonify({"ok": True})

@app.route("/api/events/stream")
@require_auth
def events_stream():
    ef = SSE_EVENTS_FILE
    def gen():
        pos = ef.stat().st_size if ef.exists() else 0
        while True:
            if ef.exists():
                try:
                    size = ef.stat().st_size
                    if size < pos: pos = 0  # rotated
                    if size > pos:
                        with open(ef) as f:
                            f.seek(pos)
                            for line in f:
                                line = line.strip()
                                if line:
                                    yield f"data: {line}\n\n"
                            pos = f.tell()
                except Exception:
                    pass
            yield f"data: {{\"type\":\"heartbeat\",\"ts\":{int(_time.time())}}}\n\n"
            _time.sleep(2)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

@app.route("/api/activity")
@require_auth
def api_activity():
    out, now = [], _time.time()
    if ACTIVITY_DIR.exists():
        import json as _json
        for f in ACTIVITY_DIR.glob("*.json"):
            try:
                r = _json.loads(f.read_text())
            except Exception:
                continue
            age = now - r.get("last", 0)
            if age > 45:                      # no heartbeat in 45s → process likely gone; sweep it
                try: f.unlink()
                except Exception: pass
                continue
            out.append({"task": r.get("task", ""), "model": r.get("model", ""),
                        "persona": r.get("persona", ""), "current": r.get("current", ""),
                        "elapsed": int(now - r.get("started", now))})
    out.sort(key=lambda x: -x["elapsed"])
    return jsonify({"active": out})

@app.route("/api/status")
@require_auth
def api_status():
    served = loaded_models()
    svcs = [{"name": n, "port": int(p), "purpose": desc, "ok": probe(h),
             "restart_key": n}
            for (n, h, p, desc) in SERVICES]
    models = [{"role": r, "id": mid, "desc": d, "ready": (mid in served)} for (r, mid, d) in ROLES]
    return jsonify({"services": svcs, "models": models, "hardware": hardware(),
                    "updated": datetime.datetime.now().strftime("%H:%M:%S")})

@app.route("/api/service-restart", methods=["POST"])
@require_auth
def api_service_restart():
    import subprocess as _sp
    key = (request.get_json(force=True, silent=True) or {}).get("key", "")
    cfg = _SVC_RESTART.get(key)
    if not cfg:
        return jsonify({"error": f"unknown service: {key}"}), 400
    try:
        if cfg["kind"] == "launchd":
            label = cfg["id"]
            _sp.run(["launchctl", "stop", label], timeout=10)
            _time.sleep(1)
            r = _sp.run(["launchctl", "start", label], timeout=10, capture_output=True, text=True)
            if r.returncode != 0:
                return jsonify({"error": r.stderr or "launchctl start failed"}), 500
        elif cfg["kind"] == "docker":
            sock = str(Path.home() / ".colima" / "default" / "docker.sock")
            r = _sp.run(["docker", "-H", f"unix://{sock}", "restart", cfg["id"]],
                        timeout=30, capture_output=True, text=True)
            if r.returncode != 0:
                return jsonify({"error": r.stderr or "docker restart failed"}), 500
        return jsonify({"ok": True, "service": key})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/vision-models")
@require_auth
def api_vision_models():
    """Return curated list of available vision models for the dropdown."""
    return jsonify({"models": [
        {"id": "mlx-community/Qwen3-VL-8B-Instruct-4bit",  "label": "Qwen3-VL-8B — SOTA small VLM (~5.4 GB, fast)"},
        {"id": "mlx-community/gemma-4-12B-it-4bit",        "label": "Gemma-4-12B — Google Vision (~6.7 GB)"},
        {"id": "mlx-community/gemma-4-31B-4bit",            "label": "Gemma-4-31B — Google Vision, best (~28.8 GB)"},
    ]})

# ── shared site nav — injected into every sub-page template ──────────────────
NAV_CSS = (
    '<style>'
    ':root{--bg:#020817;--bdr:rgba(14,165,233,.13);--blue:#0ea5e9;'
    '--text:#e2e8f0;--muted:#94a3b8;--mono:\'JetBrains Mono\',ui-monospace,monospace;'
    '--sans:\'Space Grotesk\',system-ui,sans-serif;}'
    '.snav-hdr{display:flex;align-items:center;gap:0;padding:0 20px;height:56px;'
    'border-bottom:1px solid rgba(14,165,233,.13);background:rgba(2,8,23,.95);'
    'backdrop-filter:blur(14px);position:sticky;top:0;z-index:200;flex-shrink:0;'
    'font-family:\'Space Grotesk\',system-ui,sans-serif;}'
    '.snav-logo{display:flex;align-items:center;gap:10px;margin-right:28px;text-decoration:none}'
    '.snav-dot{width:9px;height:9px;border-radius:50%;background:#0ea5e9;'
    'box-shadow:0 0 10px #0ea5e9;animation:sndot 2s infinite}'
    '@keyframes sndot{0%,100%{opacity:1}50%{opacity:.4}}'
    '.snav-name{font-family:\'JetBrains Mono\',monospace;font-weight:600;font-size:14px;'
    'letter-spacing:.08em;color:#e2e8f0}'
    '.snav-links{display:flex;gap:2px;flex:1}'
    '.snav-link{font-size:13px;font-weight:500;padding:6px 12px;border-radius:6px;'
    'color:#94a3b8;transition:.15s;text-decoration:none}'
    '.snav-link:hover{color:#e2e8f0;background:rgba(14,165,233,.1)}'
    '.snav-link.active{color:#0ea5e9;background:rgba(14,165,233,.1)}'
    '.snav-burger{display:none;background:none;border:1px solid rgba(14,165,233,.2);'
    'border-radius:6px;color:#94a3b8;cursor:pointer;font-size:17px;padding:3px 10px;'
    'line-height:1;margin-left:auto}'
    '.snav-burger:hover{color:#e2e8f0;border-color:#0ea5e9}'
    '.snav-backdrop{display:none;position:fixed;inset:0;z-index:150;background:rgba(0,0,0,.5);'
    'backdrop-filter:blur(2px)}'
    '.snav-backdrop.open{display:block}'
    '@media(max-width:680px){'
    '.snav-burger{display:flex!important;align-items:center;justify-content:center}'
    '.snav-hdr{height:52px!important;padding:0 14px;flex-wrap:nowrap}'
    '.snav-links{display:none!important;position:fixed;top:52px;left:0;right:0;'
    'flex-direction:column;background:rgba(2,8,23,.97);'
    'border-bottom:1px solid rgba(14,165,233,.15);'
    'padding:8px;gap:2px;z-index:9999;box-shadow:0 8px 32px rgba(0,0,0,.6)}'
    '.snav-links.open{display:flex!important}'
    '.snav-link{padding:11px 14px;font-size:14px;width:100%;display:block;border-radius:8px}'
    '}'
    '</style>'
)


def _build_nav(active: str = "") -> str:
    """Return header HTML block for sub-pages (CSS is injected via NAV_CSS in <head>)."""
    links = [("/","Dashboard"),("/chat","Chat"),("/vision","Vision"),
             ("/imagine","Create"),("/video","Video"),("/audio","Audio"),
             ("/personas","Personas"),("/models","Models")]
    items = "".join(
        f'<a href="{h}" class="snav-link{" active" if active==lbl.lower() or (not active and h=="/") else ""}">{lbl}</a>'
        for h, lbl in links)
    return (
        '<div class="snav-backdrop" id="snavBd" onclick="closeSnav()"></div>'
        '<header class="snav-hdr">'
        '<a class="snav-logo" href="/"><div class="snav-dot"></div>'
        '<span class="snav-name">LAW</span></a>'
        f'<nav class="snav-links" id="snav">{items}</nav>'
        '<button class="snav-burger" id="snavBurger" onclick="toggleSnav()">&#9776;</button>'
        '</header>'
        '<script>'
        'function toggleSnav(){var n=document.getElementById("snav"),b=document.getElementById("snavBd");'
        'var open=n.classList.toggle("open");b.classList.toggle("open",open);}'
        'function closeSnav(){document.getElementById("snav").classList.remove("open");'
        'document.getElementById("snavBd").classList.remove("open");}'
        'document.querySelectorAll("#snav .snav-link").forEach(function(a){'
        'a.addEventListener("click",closeSnav);});'
        '</script>'
    )

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LAW — Local AI Workstation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#020817;--surf:#0a1628;--card:rgba(13,30,53,.85);--bdr:rgba(14,165,233,.13);--bdrbrt:rgba(14,165,233,.35);
--blue:#0ea5e9;--purple:#7c3aed;--green:#10b981;--amber:#f59e0b;--red:#ef4444;
--text:#e2e8f0;--muted:#94a3b8;--mono:'JetBrains Mono',ui-monospace,monospace;--sans:'Space Grotesk',system-ui,sans-serif;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;min-height:100vh;
  background-image:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(14,165,233,.07),transparent),
    radial-gradient(ellipse 40% 30% at 90% 0%,rgba(124,58,237,.06),transparent);}
a{color:var(--blue);text-decoration:none}a:hover{color:#38bdf8}
.site-header{display:flex;align-items:center;gap:0;padding:0 20px;height:56px;
  border-bottom:1px solid var(--bdr);background:rgba(2,8,23,.8);backdrop-filter:blur(12px);
  position:sticky;top:0;z-index:100;}
.logo{display:flex;align-items:center;gap:10px;margin-right:32px}
.logo-dot{width:10px;height:10px;border-radius:50%;background:var(--blue);
  box-shadow:0 0 10px var(--blue),0 0 20px rgba(14,165,233,.4);animation:pulse 2s infinite}
.logo-name{font-family:var(--mono);font-weight:600;font-size:15px;letter-spacing:.08em;color:var(--text)}
.logo-sub{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.12em;margin-left:4px}
.nav{display:flex;gap:2px;flex:1}
.nav a{font-size:13px;font-weight:500;padding:6px 12px;border-radius:6px;color:var(--muted);transition:.15s}
.nav a:hover,.nav a.active{color:var(--text);background:rgba(14,165,233,.1)}
.nav a.active{color:var(--blue)}
.header-r{display:flex;align-items:center;gap:16px;margin-left:auto}
.status-pill{font-family:var(--mono);font-size:11px;padding:4px 10px;border-radius:20px;
  border:1px solid var(--green);color:var(--green);display:flex;align-items:center;gap:5px}
.status-pill .dot2{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 1.5s infinite}
.hclock{font-family:var(--mono);font-size:11px;color:var(--muted)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes fadeInUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
@keyframes borderGlow{0%,100%{border-color:rgba(14,165,233,.13)}50%{border-color:rgba(14,165,233,.4)}}
.main-grid{display:grid;grid-template-columns:260px 1fr 280px;gap:16px;padding:16px 20px;max-width:1400px;margin:0 auto}
@media(max-width:900px){.main-grid{grid-template-columns:1fr;}
  #pnl-sys{order:3}#pnl-neural{order:2}#pnl-cmd{order:1}}
.panel{background:var(--card);border:1px solid var(--bdr);border-radius:14px;backdrop-filter:blur(12px);
  padding:16px;display:flex;flex-direction:column;gap:12px;overflow:hidden;}
@media(max-width:900px){.panel{overflow:visible;}}
.panel:hover{border-color:var(--bdrbrt);transition:border-color .3s}
.ph{display:flex;align-items:center;gap:8px;padding-bottom:12px;border-bottom:1px solid var(--bdr)}
.ph-icon{width:20px;height:20px;border-radius:5px;background:rgba(14,165,233,.15);
  display:flex;align-items:center;justify-content:center;font-size:11px}
.ph-title{font-family:var(--mono);font-size:11px;letter-spacing:.1em;font-weight:600;color:var(--muted)}
.ph-badge{margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--muted)}
.live-b{color:var(--green);animation:pulse 2s infinite}
.stat-block{display:flex;flex-direction:column;gap:6px}
.stat-lbl{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--muted)}
.stat-bar-wrap{height:6px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden}
.stat-bar{height:100%;border-radius:3px;transition:width .6s ease;background:linear-gradient(90deg,var(--blue),#38bdf8)}
.stat-bar.cpu{background:linear-gradient(90deg,var(--purple),#a78bfa)}
.stat-bar.disk{background:linear-gradient(90deg,var(--amber),#fcd34d)}
.stat-nums{display:flex;align-items:baseline;gap:8px}
.stat-big{font-family:var(--mono);font-size:22px;font-weight:600;color:var(--text)}
.stat-det{font-family:var(--mono);font-size:11px;color:var(--muted)}
.svc-row{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.svc-row:last-child{border:none}
.svc-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.svc-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
.svc-dot.off{background:var(--red);box-shadow:0 0 6px var(--red)}
.svc-name{font-size:13px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.svc-port{font-family:var(--mono);font-size:10px;color:var(--muted)}
.svc-restart-btn{margin-left:4px;background:none;border:1px solid rgba(255,255,255,.12);color:var(--muted);font-size:11px;padding:1px 5px;border-radius:4px;cursor:pointer;line-height:1.4;flex-shrink:0}
.svc-restart-btn:hover{border-color:var(--blue);color:var(--blue)}
.svc-restart-btn:disabled{opacity:.4;cursor:not-allowed}
.feed{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px;min-height:280px;max-height:420px;scrollbar-width:thin;scrollbar-color:rgba(14,165,233,.2) transparent}
.feed-item{padding:8px 10px;border-radius:8px;background:rgba(14,165,233,.05);border-left:2px solid var(--blue);font-size:12px;animation:fadeInUp .3s ease}
.feed-item.tool{border-color:var(--purple)}
.feed-item.error{border-color:var(--red);background:rgba(239,68,68,.05)}
.feed-item.complete{border-color:var(--green);background:rgba(16,185,129,.05)}
.feed-ts{font-family:var(--mono);font-size:10px;color:var(--muted)}
.feed-agent{font-family:var(--mono);font-size:10px;font-weight:600;padding:1px 6px;border-radius:3px;margin:0 4px}
.feed-idle{color:var(--muted);font-size:12px;padding:20px 0;text-align:center}
.agent-badge{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:11px;
  padding:3px 8px;border-radius:4px;margin-right:6px}
.ab-orch{background:rgba(14,165,233,.15);color:var(--blue)}
.ab-coder{background:rgba(124,58,237,.15);color:#a78bfa}
.ab-writer{background:rgba(16,185,129,.15);color:var(--green)}
.ab-reasoner{background:rgba(245,158,11,.15);color:var(--amber)}
.ab-other{background:rgba(148,163,184,.1);color:var(--muted)}
.active-agent-row{display:flex;align-items:flex-start;gap:10px;padding:8px 10px;
  border-radius:8px;border:1px solid var(--bdr);background:rgba(14,165,233,.04);margin-bottom:6px}
.active-agent-row .spinner{width:8px;height:8px;border-radius:50%;border:2px solid rgba(14,165,233,.2);
  border-top-color:var(--blue);animation:spin .8s linear infinite;flex-shrink:0;margin-top:3px}
@keyframes spin{to{transform:rotate(360deg)}}
.act-task{font-size:12px;color:var(--text);line-height:1.4}
.act-step{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:2px}
.act-elapsed{font-family:var(--mono);font-size:10px;color:var(--muted);margin-left:auto;flex-shrink:0}
.qchat-inp{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--bdr);
  background:rgba(10,22,40,.8);color:var(--text);font:14px var(--sans);resize:none;
  outline:none;transition:.2s}
.qchat-inp:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(14,165,233,.12)}
.qchat-btn{width:100%;padding:9px;border:none;border-radius:8px;cursor:pointer;font:600 13px var(--sans);
  background:linear-gradient(135deg,var(--blue),var(--purple));color:#fff;transition:.2s;margin-top:6px}
.qchat-btn:hover{opacity:.9;transform:translateY(-1px)}
.qlinks{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
.qlink{font-size:12px;padding:5px 10px;border-radius:6px;border:1px solid var(--bdr);color:var(--muted);transition:.15s}
.qlink:hover{border-color:var(--blue);color:var(--blue)}
.spark-wrap{display:flex;flex-direction:column;gap:8px}
.spark-row{display:flex;flex-direction:column;gap:3px}
.spark-hdr{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.spark-lbl{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--muted)}
.spark-val{font-family:var(--mono);font-size:11px;color:var(--text);text-align:right}
canvas.spark{width:100%;height:24px;border-radius:4px;background:rgba(255,255,255,.02)}
.model-row{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.model-row:last-child{border:none}
.model-role{font-weight:600;font-size:13px}
.model-id{font-family:var(--mono);font-size:10px;color:var(--muted)}
.model-pill{font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:4px;margin-left:auto}
.model-pill.ready{background:rgba(16,185,129,.15);color:var(--green)}
.model-pill.idle{background:rgba(148,163,184,.08);color:var(--muted)}
.hw-info{font-family:var(--mono);font-size:10px;color:var(--muted);padding:4px 0 8px;letter-spacing:.06em}
.footer{font-family:var(--mono);font-size:10px;color:var(--muted);text-align:center;padding:12px 20px 20px;letter-spacing:.05em}
.burger{display:none;background:none;border:1px solid var(--bdr);border-radius:6px;
  color:var(--muted);cursor:pointer;font-size:18px;padding:4px 10px;line-height:1;margin-left:auto}
.burger:hover{color:var(--text);border-color:var(--blue)}
.nav-backdrop{display:none;position:fixed;inset:0;z-index:99;background:rgba(0,0,0,.5);backdrop-filter:blur(2px)}
.nav-backdrop.open{display:block}
@media(max-width:680px){
  .burger{display:flex;align-items:center;justify-content:center}
  .site-header{position:sticky;top:0;height:52px;flex-wrap:nowrap;padding:0 14px;z-index:200}
  .nav{display:none;position:fixed;top:52px;left:0;right:0;
    flex-direction:column;background:rgba(2,8,23,.97);
    border-bottom:1px solid rgba(14,165,233,.2);
    padding:8px;gap:2px;z-index:100;box-shadow:0 8px 32px rgba(0,0,0,.6)}
  .nav.open{display:flex}
  .nav a{padding:11px 14px;border-radius:8px;font-size:14px;display:block}
  .header-r{display:none}
  .main-grid{padding:10px 12px;gap:10px}
}
</style></head><body>
<div id="law-intro" style="position:fixed;inset:0;z-index:99999;background:#020817;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:0;font-family:'JetBrains Mono',monospace;overflow:hidden;transition:opacity .6s ease,transform .6s ease">
<style>
@keyframes lawGlow{0%{text-shadow:0 0 20px #0ea5e9,0 0 40px #0ea5e9;opacity:0;transform:scale(.85)}60%{opacity:1}100%{text-shadow:0 0 30px #0ea5e9,0 0 60px rgba(14,165,233,.6),0 0 100px rgba(14,165,233,.3);transform:scale(1)}}
@keyframes lawSub{0%{opacity:0;letter-spacing:.4em}100%{opacity:1;letter-spacing:.25em}}
@keyframes lawStatus{0%,49%{opacity:0}50%,100%{opacity:1}}
@keyframes lawScan{0%{top:-2px}100%{top:100%}}
@keyframes lawGrid{0%{opacity:0}100%{opacity:.04}}
#law-letters{font-size:clamp(72px,18vw,140px);font-weight:700;color:#e2e8f0;letter-spacing:.1em;animation:lawGlow 1s .3s both}
#law-sub-txt{font-size:clamp(9px,2.2vw,13px);color:#0ea5e9;letter-spacing:.25em;margin-top:10px;animation:lawSub .8s 1.1s both}
#law-status{font-size:11px;color:#10b981;margin-top:28px;animation:lawStatus .5s 1.9s infinite}
#law-scan-line{position:absolute;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,rgba(14,165,233,.6),transparent);animation:lawScan 1.5s .2s ease-in both}
#law-grid-bg{position:absolute;inset:0;background-image:linear-gradient(rgba(14,165,233,.1) 1px,transparent 1px),linear-gradient(90deg,rgba(14,165,233,.1) 1px,transparent 1px);background-size:60px 60px;animation:lawGrid 1s .1s both}
#law-corner{position:absolute;width:60px;height:60px;border-color:#0ea5e9;border-style:solid;border-width:0;opacity:.4}
#law-corner.tl{top:24px;left:24px;border-top-width:2px;border-left-width:2px}
#law-corner.tr{top:24px;right:24px;border-top-width:2px;border-right-width:2px}
#law-corner.bl{bottom:24px;left:24px;border-bottom-width:2px;border-left-width:2px}
#law-corner.br{bottom:24px;right:24px;border-bottom-width:2px;border-right-width:2px}
</style>
<div id="law-grid-bg"></div>
<div id="law-scan-line"></div>
<div id="law-corner" class="tl"></div>
<div id="law-corner" class="tr"></div>
<div id="law-corner" class="bl"></div>
<div id="law-corner" class="br"></div>
<div id="law-letters">LAW</div>
<div id="law-sub-txt">LOCAL AI WORKSTATION</div>
<div id="law-status">● SYSTEM ONLINE</div>
<script>
(function(){
  if(sessionStorage.getItem('law-intro-done')){
    document.getElementById('law-intro').style.display='none';
    return;
  }
  setTimeout(function(){
    var el=document.getElementById('law-intro');
    el.style.transition='opacity .7s ease, transform .7s ease';
    el.style.opacity='0';
    el.style.transform='translateY(-30px)';
    setTimeout(function(){el.style.display='none';},700);
    sessionStorage.setItem('law-intro-done','1');
  },3000);
})();
</script>
</div>
<div class="nav-backdrop" id="navBd" onclick="closeNav()"></div>
<header class="site-header">
  <div class="logo">
    <div class="logo-dot"></div>
    <span class="logo-name">LAW</span>
    <span class="logo-sub">LOCAL AI WORKSTATION</span>
  </div>
  <nav class="nav" id="mainNav">
    <a href="/">Dashboard</a>
    <a href="/chat">Chat</a>
    <a href="/vision">Vision</a>
    <a href="/imagine">Create</a>
    <a href="/video">Video</a>
    <a href="/audio">Audio</a>
    <a href="/personas">Personas</a>
    <a href="/models">Models</a>
  </nav>
  <div class="header-r">
    <div class="status-pill" id="statusPill"><div class="dot2"></div><span id="statusTxt">ONLINE</span></div>
    <div class="hclock" id="clock"></div>
  </div>
  <button class="burger" onclick="toggleNav()">&#9776;</button>
</header>

<div class="main-grid">
  <!-- LEFT: System Core -->
  <div class="panel" id="pnl-sys">
    <div class="ph"><div class="ph-icon">⬡</div><span class="ph-title">SYSTEM CORE</span><span class="ph-badge" id="sysTs">--:--</span></div>
    <div class="hw-info" id="hwInfo">M5 Pro · 64 GB · 1 TB</div>
    <div style="padding-top:8px;border-top:1px solid var(--bdr)">
      <div class="stat-lbl" style="margin-bottom:8px">SERVICES</div>
      <div id="svcs"></div>
    </div>
    <div style="padding-top:8px;border-top:1px solid var(--bdr)">
      <div class="stat-lbl" style="margin-bottom:8px">MODEL ROLES</div>
      <div id="modelRoles"></div>
    </div>
  </div>

  <!-- CENTER: Neural Activity Feed -->
  <div class="panel" id="pnl-neural">
    <div class="ph">
      <div class="ph-icon" style="background:rgba(16,185,129,.15)">◉</div>
      <span class="ph-title">NEURAL ACTIVITY</span>
      <span class="ph-badge live-b" id="feedStatus">● LIVE</span>
      <button onclick="clearAllCache()" title="Clear neural activity feed and chat history"
        style="margin-left:auto;padding:2px 9px;border-radius:5px;border:1px solid rgba(239,68,68,.3);
        background:rgba(239,68,68,.07);color:#ef4444;font-size:10px;cursor:pointer;font-family:var(--mono);
        transition:.15s" onmouseover="this.style.background='rgba(239,68,68,.18)'"
        onmouseout="this.style.background='rgba(239,68,68,.07)'">✕ CLEAR</button>
    </div>
    <div style="padding-bottom:8px;border-bottom:1px solid var(--bdr)">
      <div class="stat-lbl" style="margin-bottom:6px">ACTIVE AGENTS</div>
      <div id="activeAgents"><div class="feed-idle">No active agents — standing by</div></div>
    </div>
    <div class="feed" id="eventFeed">
      <div class="feed-idle">Waiting for agent events…</div>
    </div>
  </div>

  <!-- RIGHT: Command Panel -->
  <div class="panel" id="pnl-cmd">
    <div class="ph"><div class="ph-icon" style="background:rgba(124,58,237,.15)">◈</div><span class="ph-title">COMMAND</span></div>
    <textarea class="qchat-inp" id="quickIn" rows="3" placeholder="Quick message to orchestrator…"></textarea>
    <button class="qchat-btn" onclick="quickSend()">DISPATCH →</button>
    <div style="font-family:var(--mono);font-size:10px;color:var(--muted);padding:2px 0" id="qStatus"></div>
    <div class="qlinks">
      <a href="/chat" class="qlink">💬 Full Chat</a>
      <a href="/vision" class="qlink">👁 Vision</a>
      <a href="/imagine" class="qlink">🎨 Create</a>
      <a href="/personas" class="qlink">🎭 Personas</a>
    </div>
    <div style="padding-top:8px;border-top:1px solid var(--bdr)">
      <div class="stat-lbl" style="margin-bottom:8px">LIVE METRICS</div>
      <div class="spark-wrap">
        <div class="spark-row">
          <div class="spark-hdr"><span class="spark-lbl">CPU</span><span class="spark-val" id="sCpuV">--%</span></div>
          <canvas class="spark" id="sCpu"></canvas>
        </div>
        <div class="spark-row">
          <div class="spark-hdr"><span class="spark-lbl">MEMORY</span><span class="spark-val" id="sRamV">-- · --/-- GB</span></div>
          <canvas class="spark" id="sRam"></canvas>
        </div>
        <div class="spark-row">
          <div class="spark-hdr"><span class="spark-lbl">BATTERY</span><span class="spark-val" id="sBatV">-- · --</span></div>
          <canvas class="spark" id="sBat"></canvas>
        </div>
        <div class="spark-row">
          <div class="spark-hdr"><span class="spark-lbl">STORAGE</span><span class="spark-val" id="sStrV">-- · --/-- GB</span></div>
          <canvas class="spark" id="sStr"></canvas>
        </div>
        <div class="spark-row">
          <div class="spark-hdr"><span class="spark-lbl">DISK I/O</span><span class="spark-val" id="sDskV">0 MB/s</span></div>
          <canvas class="spark" id="sDsk"></canvas>
        </div>
        <div class="spark-row">
          <div class="spark-hdr"><span class="spark-lbl">NETWORK</span><span class="spark-val" id="sNetV">0 MB/s</span></div>
          <canvas class="spark" id="sNet"></canvas>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="footer" id="foot">LAW · LOCAL AI WORKSTATION · 100% LOCAL · $0 API</div>

<script>
const HOST=window.location.hostname||'localhost';
// auto-set active nav link
document.querySelectorAll('.nav a').forEach(function(a){
  a.classList.toggle('active', a.getAttribute('href')===window.location.pathname);
});
// floating mobile nav
function toggleNav(){var n=document.getElementById('mainNav'),b=document.getElementById('navBd');var o=n.classList.toggle('open');b.classList.toggle('open',o);}
function closeNav(){document.getElementById('mainNav').classList.remove('open');document.getElementById('navBd').classList.remove('open');}
document.querySelectorAll('#mainNav a').forEach(function(a){a.addEventListener('click',closeNav);});
function su(port){return 'http://'+HOST+':'+port;}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function ts(){const n=new Date();return n.toLocaleTimeString('en-GB',{hour12:false});}
setInterval(()=>{document.getElementById('clock').textContent=ts();},1000);
document.getElementById('clock').textContent=ts();

function agentBadge(name){
  const n=(name||'').toLowerCase();
  const cls=n.includes('orch')?'ab-orch':n.includes('cod')||n.includes('dev')?'ab-coder':
    n.includes('writ')||n.includes('doc')?'ab-writer':n.includes('reason')||n.includes('think')?'ab-reasoner':'ab-other';
  return '<span class="agent-badge '+cls+'">'+esc(name||'agent')+'</span>';
}

async function pollStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    const h=d.hardware;
    document.getElementById('sysTs').textContent=d.updated||'';
    // compact hw info line
    const ramTotalGB=Math.round((h.ram_txt||'').split('/').pop()||64);
    document.getElementById('hwInfo').textContent=
      'M5 Pro · '+(h.ram_txt||'').split('/').pop().trim()+' RAM · '+(h.disk_txt||'').split('/').pop().trim()+' disk';
    const up=d.services.filter(s=>s.ok).length;
    document.getElementById('svcs').innerHTML=d.services.map(s=>
      '<div class="svc-row" id="svcrow-'+s.port+'"><div class="svc-dot '+(s.ok?'on':'off')+'" id="svcdot-'+s.port+'"></div>'+
      '<a class="svc-name" href="'+su(s.port)+'" target="_blank">'+esc(s.name)+'</a>'+
      '<span class="svc-port">:'+s.port+'</span>'+
      '<button class="svc-restart-btn" title="Restart service" onclick="restartSvc(this,\''+esc(s.restart_key||s.name)+'\','+s.port+')">↺</button>'+
      '</div>').join('');
    document.getElementById('modelRoles').innerHTML=d.models.map(m=>
      '<div class="model-row"><div><div class="model-role">'+esc(m.role)+'</div><div class="model-id">'+esc(m.id)+'</div></div>'+
      '<span class="model-pill '+(m.ready?'ready':'idle')+'">'+(m.ready?'loaded':'on demand')+'</span></div>').join('');
    document.getElementById('foot').textContent='LAW · '+up+'/'+d.services.length+' services online · M5 Pro · 100% LOCAL';
    document.getElementById('statusPill').style.borderColor=up>0?'var(--green)':'var(--red)';
    document.getElementById('statusPill').querySelector('.dot2').style.background=up>0?'var(--green)':'var(--red)';
    document.getElementById('statusTxt').textContent=up>0?'ONLINE':'OFFLINE';
  }catch(e){document.getElementById('foot').textContent='Dashboard offline — check services';}
}
pollStatus();setInterval(pollStatus,5000);

async function restartSvc(btn,key,port){
  btn.disabled=true;btn.textContent='⏳';
  try{
    const r=await fetch('/api/service-restart',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key})});
    const d=await r.json();
    if(r.ok){
      btn.textContent='✓';btn.style.color='var(--green)';
      setTimeout(()=>{btn.textContent='↺';btn.style.color='';btn.disabled=false;pollStatus();},3000);
    }else{
      btn.textContent='✗';btn.style.color='var(--red)';btn.title=d.error||'failed';
      setTimeout(()=>{btn.textContent='↺';btn.style.color='';btn.title='Restart service';btn.disabled=false;},4000);
    }
  }catch(e){btn.textContent='↺';btn.style.color='';btn.disabled=false;}
}

async function pollActivity(){
  try{
    const d=await(await fetch('/api/activity')).json();
    const el=document.getElementById('activeAgents');
    if(!d.active||!d.active.length){el.innerHTML='<div class="feed-idle">No active agents — standing by</div>';return;}
    el.innerHTML=d.active.map(t=>
      '<div class="active-agent-row"><div class="spinner"></div><div style="flex:1">'+
      agentBadge(t.persona||t.model)+
      '<span class="act-task">'+esc(t.task)+'</span>'+
      (t.current?'<div class="act-step">▸ '+esc(t.current)+'</div>':'')+
      '</div><span class="act-elapsed">'+t.elapsed+'s</span></div>').join('');
  }catch(e){}
}
pollActivity();setInterval(pollActivity,3000);

// SSE event feed
const MAX_FEED=80;
const feedEl=document.getElementById('eventFeed');
const FEED_KEY='law_feed_v1';
let feedItems=[];
try{const saved=JSON.parse(localStorage.getItem(FEED_KEY)||'[]');if(Array.isArray(saved)&&saved.length){feedItems=saved.slice(-MAX_FEED);feedEl.innerHTML=feedItems.join('');feedEl.scrollTop=feedEl.scrollHeight;document.getElementById('feedStatus').textContent='● RESTORED';setTimeout(()=>{document.getElementById('feedStatus').textContent='● LIVE';},2000);}}catch(e){}
function addFeedItem(ev){
  if(ev.type==='heartbeat')return;
  const ts2=ev.ts?new Date(ev.ts*1000).toLocaleTimeString('en-GB',{hour12:false}):ts();
  const cls=ev.type==='error'?'error':ev.type==='complete'?'complete':ev.type==='tool_use'?'tool':'';
  const html='<div class="feed-item '+cls+'">'+
    '<span class="feed-ts">'+ts2+'</span>'+
    agentBadge(ev.agent)+
    '<span>'+esc(ev.data||ev.type)+'</span></div>';
  feedItems.push(html);
  if(feedItems.length>MAX_FEED)feedItems.shift();
  try{localStorage.setItem(FEED_KEY,JSON.stringify(feedItems));}catch(e){}
  const paused=feedEl.scrollHeight-feedEl.scrollTop-feedEl.clientHeight>60;
  feedEl.innerHTML=feedItems.join('');
  if(!paused)feedEl.scrollTop=feedEl.scrollHeight;
}
function startSSE(){
  const es=new EventSource('/api/events/stream');
  es.onmessage=e=>{try{addFeedItem(JSON.parse(e.data));}catch(ex){}};
  es.onerror=()=>{
    document.getElementById('feedStatus').textContent='● RECONNECTING';
    setTimeout(startSSE,4000);es.close();
  };
}
startSSE();

async function clearAllCache(){
  if(!confirm('Clear all neural activity and chat history?'))return;
  try{await fetch('/api/clear-cache',{method:'POST'});}catch(e){}
  feedItems=[];
  feedEl.innerHTML='<div class="feed-idle">Cache cleared.</div>';
  try{localStorage.removeItem(FEED_KEY);}catch(e){}
  try{sessionStorage.removeItem('law_chat_v2');}catch(e){}
  const st=document.getElementById('feedStatus');
  st.textContent='● CLEARED';st.style.color='var(--red)';
  setTimeout(()=>{st.textContent='● LIVE';st.style.color='';},2500);
}

// Metrics + sparklines
function drawSpark(id,vals,color){
  const cv=document.getElementById(id);if(!cv)return;
  cv.width=cv.clientWidth||120;const h=cv.height=28,w=cv.width,ctx=cv.getContext('2d');
  ctx.clearRect(0,0,w,h);
  if(!vals.length)return;
  const xs=w/Math.max(vals.length-1,1);
  const yof=v=>h-(Math.min(100,Math.max(0,v))/100)*h;
  const grad=ctx.createLinearGradient(0,0,0,h);
  grad.addColorStop(0,color+'66');grad.addColorStop(1,color+'00');
  ctx.beginPath();vals.forEach((v,i)=>{const x=i*xs,y=yof(v);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.lineTo((vals.length-1)*xs,h);ctx.lineTo(0,h);ctx.closePath();
  ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();vals.forEach((v,i)=>{const x=i*xs,y=yof(v);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
  ctx.strokeStyle=color;ctx.lineWidth=1.5;ctx.stroke();
}
async function pollMetrics(){
  try{
    const m=await(await fetch('/api/metrics')).json();
    const last=a=>a&&a.length?a[a.length-1]:0;
    const cpu=m.cpu||[],ram=m.ram||[],ramGb=m.ram_gb||[];
    const dr=m.disk_r||[],dw=m.disk_w||[];
    const nr=m.net_r||[],ns=m.net_s||[];
    const bat=m.bat||[];
    function merge(a,b){return a.map((v,i)=>v+(b[i]||0));}
    const diskIO=merge(dr,dw), net=merge(nr,ns);
    const batPct=last(bat);const batChg=m.battery&&m.battery.charging;
    const ramTot=m.ram_total_gb||64;
    const diskPct=m.disk_pct||0,diskUsed=m.disk_used_gb||0,diskTot=m.disk_total_gb||0;
    // Update value labels with pct + actual
    document.getElementById('sCpuV').textContent=last(cpu)+'%';
    document.getElementById('sRamV').textContent=last(ram)+'% · '+last(ramGb)+'/'+ramTot+' GB';
    if(batPct>=0){
      const batTime=m.battery&&m.battery.time_str?m.battery.time_str:'';
      document.getElementById('sBatV').textContent=batPct+'%'+(batChg?' ⚡ charging':batTime?' · '+batTime:'');
    }else{document.getElementById('sBatV').textContent='—';}
    document.getElementById('sStrV').textContent=diskPct+'% · '+diskUsed+'/'+diskTot+' GB';
    const dv=last(diskIO),nv=last(net);
    document.getElementById('sDskV').textContent=dv>1?dv.toFixed(0)+' MB/s':dv.toFixed(2)+' MB/s';
    document.getElementById('sNetV').textContent=nv>1?nv.toFixed(0)+' MB/s':nv.toFixed(2)+' MB/s';
    // Draw sparklines
    const normSpark=(vals)=>{const mx=Math.max(...vals,0.01);return vals.map(v=>v/mx*100);};
    // storage pct array: fill with constant current value (no history, but shows as flat line)
    const storArr=Array(20).fill(diskPct);
    drawSpark('sCpu',cpu.slice(-20),'#7c3aed');
    drawSpark('sRam',ram.slice(-20),'#0ea5e9');
    drawSpark('sBat',bat.slice(-20).map(v=>v>=0?v:0),batChg?'#10b981':'#f59e0b');
    drawSpark('sStr',storArr,'#94a3b8');
    drawSpark('sDsk',normSpark(diskIO.slice(-20)),'#f59e0b');
    drawSpark('sNet',normSpark(net.slice(-20)),'#10b981');
  }catch(e){}
}
pollMetrics();setInterval(pollMetrics,3000);

async function quickSend(){
  const inp=document.getElementById('quickIn');const txt=inp.value.trim();if(!txt)return;
  const st=document.getElementById('qStatus');
  st.textContent='Dispatching…';inp.disabled=true;
  try{
    const r=await fetch('/api/agent-chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({persona:'orchestrator',message:txt,history:[]})});
    if(r.status===429){st.textContent='Agent busy — try Full Chat';inp.disabled=false;return;}
    const reader=r.body.getReader();const dec=new TextDecoder();let buf='',ans='';
    while(true){const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});let i;
      while((i=buf.indexOf('\n'))>=0){const ln=buf.slice(0,i);buf=buf.slice(i+1);
        try{const o=JSON.parse(ln);if(o.type==='answer')ans=o.text;}catch(ex){}}}
    st.textContent=ans?'Done — see Full Chat for details':'Done';
    inp.value='';
  }catch(e){st.textContent='Error: '+e;}
  inp.disabled=false;
}
document.getElementById('quickIn').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();quickSend();}});
</script></body></html>"""

LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8><title>LAW - Sign In</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Space Grotesk',system-ui,sans-serif;background:#020817;color:#e2e8f0;
  display:grid;place-items:center;height:100vh;
  background-image:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(14,165,233,.08),transparent);}
.card{background:rgba(13,30,53,.9);border:1px solid rgba(14,165,233,.2);border-radius:16px;
  padding:36px;width:min(340px,92vw);backdrop-filter:blur(16px);
  box-shadow:0 0 60px rgba(14,165,233,.08),0 20px 60px #0008;}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
.ldot{width:10px;height:10px;border-radius:50%;background:#0ea5e9;box-shadow:0 0 12px #0ea5e9;animation:p 2s infinite}
.lname{font-family:'JetBrains Mono',monospace;font-weight:600;font-size:16px;letter-spacing:.08em}
.lsub{font-family:'JetBrains Mono',monospace;font-size:10px;color:#94a3b8;letter-spacing:.1em}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
label{display:block;font-size:12px;color:#94a3b8;margin-bottom:6px;font-family:'JetBrains Mono',monospace;letter-spacing:.06em}
input{width:100%;padding:11px 14px;border-radius:8px;border:1px solid rgba(14,165,233,.2);
  background:rgba(2,8,23,.8);color:#e2e8f0;font:15px 'Space Grotesk',system-ui;outline:none;transition:.2s}
input:focus{border-color:#0ea5e9;box-shadow:0 0 0 3px rgba(14,165,233,.12)}
button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:8px;
  background:linear-gradient(135deg,#0ea5e9,#7c3aed);color:#fff;font:600 14px 'Space Grotesk',system-ui;cursor:pointer;transition:.2s}
button:hover{opacity:.9;transform:translateY(-1px)}
.err{color:#ef4444;font-size:12px;margin-top:10px;min-height:16px;font-family:'JetBrains Mono',monospace}
</style></head>
<body><div class=card><div class=logo><div class=ldot></div><div><div class=lname>LAW</div><div class=lsub>LOCAL AI WORKSTATION</div></div></div>
<label>ACCESS KEY</label>
<input type=password name=password placeholder="Enter password" autofocus form=f>
<div class=err><!--err--></div>
<form id=f method=post><button>AUTHENTICATE →</button><input type=hidden name=password></form>
<script>document.querySelector('input[type=password]').oninput=e=>document.querySelector('input[type=hidden]').value=e.target.value;</script>
</div></body></html>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ON: return redirect("/")
    if request.method == "POST":
        if DASH_PASSWORD and hmac.compare_digest(request.form.get("password", ""), DASH_PASSWORD):
            session["ok"] = True; return redirect("/")
        return Response(LOGIN_HTML.replace("<!--err-->", "Wrong password"), mimetype="text/html")
    return Response(LOGIN_HTML, mimetype="text/html")

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login" if AUTH_ON else "/")

# ── persona management (thin layer over the agent's tested data functions) ──
def _gateway_models():
    try:
        r = requests.get(f"http://127.0.0.1:{P_GATEWAY}/v1/models", timeout=3)
        return sorted(m.get("id", "") for m in r.json().get("data", []))
    except Exception:
        return []

def _tool_names():
    return sorted(agent.ALL_SCHEMAS.keys()) if agent else []

def _starters():
    return set(agent.DEFAULT_PERSONAS.keys()) if agent else set()

@app.route("/api/personas")
@require_auth
def api_personas():
    if not agent: return jsonify({"error": "agent module not loaded"}), 500
    p = agent.load_personas(); st = _starters()
    out = [{"name": n, "description": c.get("description", ""), "model": c.get("model", ""),
            "allowed_tools": c.get("allowed_tools", "all"), "approval": c.get("approval", "normal"),
            "system_prompt": c.get("system_prompt", ""), "starter": n in st}
           for n, c in p.items()]
    return jsonify({"personas": out, "models": _gateway_models(), "tools": _tool_names()})

@app.route("/api/personas/save", methods=["POST"])
@require_auth
def api_personas_save():
    if not agent: return jsonify({"error": "agent module not loaded"}), 500
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", name):
        return jsonify({"error": "name must be 1–32 chars: letters, digits, _ or -"}), 400
    model = (d.get("model") or "").strip()
    if not model:
        return jsonify({"error": "pick a model"}), 400
    tools = d.get("allowed_tools", "all")
    if isinstance(tools, list):
        tools = "all" if ("all" in tools or not tools) else [t for t in tools if t in agent.ALL_SCHEMAS]
    entry = {"description": (d.get("description") or "").strip(), "model": model,
             "allowed_tools": tools, "system_prompt": (d.get("system_prompt") or "").strip()}
    if d.get("approval") == "strict": entry["approval"] = "strict"
    p = agent.load_personas(); p[name] = entry; agent.save_personas(p)
    return jsonify({"ok": True})

@app.route("/api/personas/delete", methods=["POST"])
@require_auth
def api_personas_delete():
    if not agent: return jsonify({"error": "agent module not loaded"}), 500
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if name in _starters():
        return jsonify({"error": "built-in starter — edit it instead of deleting"}), 400
    p = agent.load_personas()
    if name in p: del p[name]; agent.save_personas(p)
    return jsonify({"ok": True})

PERSONA_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Personas - MLX</title>
<meta name=viewport content="width=device-width,initial-scale=1"><!--NAV_CSS--><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;padding:0;overflow-y:auto}
a{color:#60a5fa;text-decoration:none}h1{font-size:20px;margin:0 0 4px}.sub{color:#8b98b8;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.card{background:#141b2e;border:1px solid #223052;border-radius:12px;padding:14px}
.card h3{margin:0 0 4px;font-size:16px}.tag{display:inline-block;font-size:11px;padding:2px 7px;border-radius:20px;background:#1e293b;color:#93c5fd;margin-right:4px}
.strict{background:#3b1d1d;color:#fca5a5}.mono{font-family:ui-monospace,monospace;font-size:12px;color:#9fb3d1}
.desc{color:#b6c2da;font-size:13px;margin:6px 0}.row{margin-top:10px}
button{border:0;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;font-size:13px}
.edit{background:#334155;color:#e6edf3}.del{background:#7f1d1d;color:#fee2e2;margin-left:6px}.add{background:#3b82f6;color:#fff}
dialog{background:#141b2e;color:#e6edf3;border:1px solid #223052;border-radius:14px;
  padding:20px;width:min(560px,94vw);max-height:88vh;overflow-y:auto}
dialog[open]{display:flex;flex-direction:column;gap:0}
dialog::backdrop{background:rgba(0,0,0,.7);backdrop-filter:blur(4px)}
label{display:block;font-size:13px;margin:10px 0 4px;color:#b6c2da}
input,select,textarea{width:100%;padding:9px;border-radius:8px;border:1px solid #2a3550;background:#0b1020;color:#e6edf3;font:14px system-ui}
textarea{min-height:80px;max-height:240px;resize:vertical}
.tools{display:flex;flex-wrap:wrap;gap:8px;max-height:140px;overflow-y:auto;border:1px solid #2a3550;border-radius:8px;padding:8px}
.tools label{display:flex;align-items:center;gap:5px;margin:0;font-size:12px}.tools input{width:auto}
.err{color:#f87171;font-size:13px;min-height:16px;margin-top:8px}
.actions{margin-top:16px;display:flex;justify-content:flex-end;gap:8px;flex-shrink:0;position:sticky;bottom:0;background:#141b2e;padding:10px 0 0}
@media(max-width:600px){
  dialog{padding:14px;border-radius:10px;max-height:92vh}
  .grid{grid-template-columns:1fr}
  textarea{min-height:60px;max-height:160px}
  .tools{max-height:100px}
  input,select,textarea{font-size:16px}
}
</style></head><body>
<!--NAV-->
<div style="padding:16px 20px 24px">
<h1>🎭 Personas</h1><div class=sub>Each persona = a model + system prompt + tool permissions + approval level. Used by the agent & Telegram (not the web-chat dropdown).</div>
<p><button class=add onclick="openEdit()">+ New persona</button></p>
<div class=grid id=grid></div>
<dialog id=dlg><h3 id=dtitle>New persona</h3>
<label>Name (letters, digits, _ -)</label><input id=f_name>
<label>Description</label><input id=f_desc>
<label>Model</label><select id=f_model></select>
<label>Approval</label><select id=f_appr><option value=normal>normal (can allow-all per run)</option><option value=strict>strict (always ask)</option></select>
<label>Allowed tools</label>
<div><label style="display:inline-flex;gap:6px"><input type=checkbox id=f_all onchange="toggleAll()"> all tools</label></div>
<div class=tools id=f_tools></div>
<label>System prompt</label><textarea id=f_sys></textarea>
<div class=err id=err></div>
<div class=actions><button class=edit onclick="dlg.close()">Cancel</button><button class=add onclick="save()">Save</button></div>
</dialog>
<script>
let MODELS=[],TOOLS=[],editing=null;
async function load(){
  const r=await fetch('/api/personas'); if(r.status==401){location='/login';return;}
  const d=await r.json(); MODELS=d.models||[]; TOOLS=d.tools||[];
  const g=document.getElementById('grid'); g.innerHTML='';
  (d.personas||[]).forEach(p=>{
    const tools=Array.isArray(p.allowed_tools)?p.allowed_tools.join(', '):'all tools';
    const el=document.createElement('div'); el.className='card';
    el.innerHTML=`<h3>${p.name} ${p.starter?'<span class=tag>starter</span>':''} ${p.approval=='strict'?'<span class="tag strict">strict</span>':''}</h3>
      <div class=mono>${p.model||'—'}</div><div class=desc>${p.description||''}</div>
      <div class=mono>🔧 ${tools}</div>
      <div class=row><button class=edit>Edit</button>${p.starter?'':'<button class=del>Delete</button>'}</div>`;
    el.querySelector('.edit').onclick=()=>openEdit(p);
    const db=el.querySelector('.del'); if(db) db.onclick=()=>del(p.name);
    g.appendChild(el);
  });
}
function openEdit(p){
  editing=p||null; document.getElementById('err').textContent='';
  document.getElementById('dtitle').textContent=p?('Edit '+p.name):'New persona';
  const mSel=document.getElementById('f_model'); mSel.innerHTML=MODELS.map(m=>`<option>${m}</option>`).join('');
  const tw=document.getElementById('f_tools'); tw.innerHTML=TOOLS.map(t=>`<label><input type=checkbox value="${t}">${t}</label>`).join('');
  const isAll = !p || p.allowed_tools==='all' || !Array.isArray(p.allowed_tools);
  document.getElementById('f_all').checked=isAll; toggleAll();
  document.getElementById('f_name').value=p?p.name:''; document.getElementById('f_name').disabled=!!p;
  document.getElementById('f_desc').value=p?p.description:''; 
  if(p&&p.model)mSel.value=p.model;
  document.getElementById('f_appr').value=p?p.approval:'normal';
  document.getElementById('f_sys').value=p?p.system_prompt:'';
  if(p&&Array.isArray(p.allowed_tools))[...tw.querySelectorAll('input')].forEach(c=>c.checked=p.allowed_tools.includes(c.value));
  document.getElementById('dlg').showModal();
}
function toggleAll(){const on=document.getElementById('f_all').checked;document.getElementById('f_tools').style.opacity=on?0.4:1;
  [...document.querySelectorAll('#f_tools input')].forEach(c=>c.disabled=on);}
async function save(){
  const all=document.getElementById('f_all').checked;
  const tools=all?'all':[...document.querySelectorAll('#f_tools input:checked')].map(c=>c.value);
  const body={name:document.getElementById('f_name').value.trim(),description:document.getElementById('f_desc').value,
    model:document.getElementById('f_model').value,approval:document.getElementById('f_appr').value,
    allowed_tools:tools,system_prompt:document.getElementById('f_sys').value};
  const r=await fetch('/api/personas/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); if(!r.ok){document.getElementById('err').textContent=d.error||'error';return;}
  document.getElementById('dlg').close(); load();
}
async function del(name){ if(!confirm('Delete persona '+name+'?'))return;
  await fetch('/api/personas/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}); load();}
load();
</script></div></body></html>"""

@app.route("/personas")
@require_auth
def personas_page():
    return Response(PERSONA_PAGE.replace('<!--NAV_CSS-->', NAV_CSS).replace('<!--NAV-->', _build_nav('personas')), mimetype="text/html")

# ── model management (thin layer over the installer's tested commands) ──────
import subprocess, threading as _th, time as _time
SETUP = ENV.get("MLX_SETUP_PATH", "")            # recorded during --bootstrap
_JOBS = {}                                       # id -> {stage, log[], done, ok, repo}
_JOB_LOCK = _th.Lock()

def _registry_rows():
    reg = WORKDIR / "models.custom.tsv"; rows = []
    if reg.exists():
        for ln in reg.read_text().splitlines():
            parts = ln.split("\t")
            if len(parts) >= 3: rows.append({"role": parts[0], "repo": parts[1], "type": parts[2]})
    return rows

@app.route("/api/models")
@require_auth
def api_models():
    served = []
    try:
        r = requests.get(f"http://127.0.0.1:{P_MLX}/v1/models", timeout=3)
        served = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception: pass
    return jsonify({"served": sorted(served), "custom": _registry_rows(),
                    "gateway": _gateway_models(), "setup_ok": bool(SETUP)})

def _cached_repos():
    # scan the HF hub cache for fully-downloaded repos → repo ids
    base = os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME")
    hub = Path(base) / "hub" if base and not base.rstrip("/").endswith("hub") else \
          (Path(base) if base else Path.home() / ".cache" / "huggingface" / "hub")
    if not hub.exists():
        hub = Path.home() / ".cache" / "huggingface" / "hub"
    out = []
    if hub.exists():
        for d in hub.iterdir():
            if d.is_dir() and d.name.startswith("models--"):
                snap = d / "snapshots"
                if snap.exists() and any(snap.iterdir()):
                    out.append(d.name[len("models--"):].replace("--", "/"))
    return sorted(out)

@app.route("/api/models/cached")
@require_auth
def api_models_cached():
    reg = {r["repo"] for r in _registry_rows()}
    return jsonify({"cached": [{"repo": r, "registered": (r in reg)} for r in _cached_repos()]})

@app.route("/api/models/search")
@require_auth
def api_models_search():
    q = (request.args.get("q") or "").strip()
    if not q: return jsonify({"results": []})
    try:  # Hugging Face public search API, filtered to MLX-format repos
        r = requests.get("https://huggingface.co/api/models",
                         params={"search": q, "filter": "mlx", "sort": "downloads",
                                 "direction": "-1", "limit": 25},
                         timeout=12)
        out = [{"id": m.get("id", ""), "downloads": m.get("downloads", 0), "likes": m.get("likes", 0)}
               for m in r.json()]
        return jsonify({"results": out})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)}), 502

def _job_log(jid, line):
    with _JOB_LOCK:
        if jid in _JOBS: _JOBS[jid]["log"].append(line)

def _run_add_job(jid, repo, role, mtype, tparse, rparse):
    def stage(s):
        with _JOB_LOCK:
            if jid in _JOBS: _JOBS[jid]["stage"] = s
        _job_log(jid, f"— {s} —")
    try:
        if not SETUP or not os.path.exists(SETUP):
            _job_log(jid, "ERROR: installer path unknown (re-run --bootstrap)"); return _finish(jid, False)
        # 1) download (no register yet)
        stage("downloading")
        if _sh(jid, [SETUP, "--download-model", repo]) != 0:
            _job_log(jid, "download failed"); return _finish(jid, False)
        # 2) LOAD-PROBE before registering (searchable ≠ loadable)
        stage("probing (loading the model to confirm it runs)")
        rc = _sh(jid, [SETUP, "--probe-model", repo, mtype])
        if rc != 0:
            _job_log(jid, "✗ probe failed — NOT added to the dropdown (weights kept in cache)")
            return _finish(jid, False)
        # 3) only now register → dropdown
        stage("registering")
        args = [SETUP, "--add-model", repo, role, mtype, tparse, rparse]
        if _sh(jid, args) != 0:
            _job_log(jid, "register failed"); return _finish(jid, False)
        _job_log(jid, f"✓ '{role}' probed loadable and added to the dropdown")
        _finish(jid, True)
    except Exception as e:
        _job_log(jid, f"error: {e}"); _finish(jid, False)

def _sh(jid, args):
    try:
        p = subprocess.Popen(["bash"] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            _job_log(jid, line.rstrip()[:400])
        p.wait(); return p.returncode
    except Exception as e:
        _job_log(jid, f"exec error: {e}"); return 1

def _finish(jid, ok):
    with _JOB_LOCK:
        if jid in _JOBS: _JOBS[jid]["done"] = True; _JOBS[jid]["ok"] = ok

@app.route("/api/models/add", methods=["POST"])
@require_auth
def api_models_add():
    d = request.get_json(force=True, silent=True) or {}
    repo = (d.get("repo") or "").strip()
    role = (d.get("role") or "").strip()
    mtype = (d.get("type") or "lm").strip()
    if not repo or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]+$", repo):
        return jsonify({"error": "bad repo id"}), 400
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", role):
        return jsonify({"error": "role: lowercase letters/digits/hyphen, ≤31 chars"}), 400
    jid = secrets.token_hex(4)
    with _JOB_LOCK:
        _JOBS[jid] = {"stage": "queued", "log": [], "done": False, "ok": False, "repo": repo}
    _th.Thread(target=_run_add_job, args=(jid, repo, role, mtype,
               (d.get("tool_parser") or "").strip(), (d.get("reasoning_parser") or "").strip()),
               daemon=True).start()
    return jsonify({"job": jid})

@app.route("/api/models/job/<jid>")
@require_auth
def api_models_job(jid):
    with _JOB_LOCK:
        j = _JOBS.get(jid)
        if not j: return jsonify({"error": "no such job"}), 404
        return jsonify(dict(j))

@app.route("/api/models/remove", methods=["POST"])
@require_auth
def api_models_remove():
    d = request.get_json(force=True, silent=True) or {}
    role = (d.get("role") or "").strip()
    if not SETUP: return jsonify({"error": "installer path unknown"}), 500
    # echo 'n' so the installer keeps the weights (non-interactive)
    try:
        p = subprocess.run(f'echo n | bash {subprocess.list2cmdline([SETUP])} --remove-model {subprocess.list2cmdline([role])}',
                           shell=True, capture_output=True, text=True, timeout=120)
        return jsonify({"ok": p.returncode == 0, "log": (p.stdout + p.stderr)[-2000:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

MODELS_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Models - MLX</title>
<meta name=viewport content="width=device-width,initial-scale=1"><!--NAV_CSS--><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;padding:0;overflow-y:auto}
a{color:#60a5fa;text-decoration:none}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;color:#93c5fd;margin:22px 0 8px}
.sub{color:#8b98b8;font-size:13px;margin-bottom:16px}.mono{font-family:ui-monospace,monospace;font-size:12px}
.bar{display:flex;gap:8px}input,select{padding:9px;border-radius:8px;border:1px solid #2a3550;background:#0b1020;color:#e6edf3;font:14px system-ui}
input#q{flex:1}button{border:0;border-radius:8px;padding:8px 13px;font-weight:600;cursor:pointer;font-size:13px}
.go{background:#3b82f6;color:#fff}.rm{background:#7f1d1d;color:#fee2e2}.add{background:#166534;color:#dcfce7}
.card{background:#141b2e;border:1px solid #223052;border-radius:10px;padding:11px 13px;margin:8px 0;display:flex;justify-content:space-between;align-items:center;gap:10px}
.tag{font-size:11px;color:#93c5fd;background:#1e293b;padding:2px 7px;border-radius:20px}
dialog{background:#141b2e;color:#e6edf3;border:1px solid #223052;border-radius:14px;padding:20px;width:min(520px,92vw)}
label{display:block;font-size:13px;margin:10px 0 4px;color:#b6c2da}dialog input,dialog select{width:100%}
.actions{margin-top:16px;display:flex;justify-content:flex-end;gap:8px}.err{color:#f87171;font-size:13px;min-height:16px}
#joblog{font-family:ui-monospace,monospace;font-size:12px;background:#05070f;border:1px solid #223052;border-radius:10px;padding:12px;max-height:340px;overflow:auto;white-space:pre-wrap}
.stage{color:#fbbf24}.warn{color:#fca5a5;font-size:12px}
</style></head><body>
<!--NAV-->
<div style="padding:20px 24px 24px">
<h1>📦 Models</h1>
<div class=sub>Search Hugging Face (MLX only) → download → <b>load-probe</b> (actually runs it) → add to the dropdown. A model that downloads but won't load never gets registered.</div>
<div id=warn class=warn></div>
<div class=bar><input id=q placeholder="search HF for MLX models, e.g. qwen coder, llama, whisper" onkeydown="if(event.key=='Enter')search()"><button class=go onclick=search()>Search</button></div>
<div id=results></div>
<h2>In your local cache <span class=sub>(downloaded but not yet added — one click to probe + register)</span></h2><div id=cached></div>
<h2>Currently registered</h2><div id=current></div>
<dialog id=dlg><h3 id=dt>Add model</h3>
<label>HF repo</label><input id=f_repo readonly>
<label>Role / dropdown name (lowercase)</label><input id=f_role>
<label>Type</label><select id=f_type><option value=lm>lm (text / chat / coder)</option><option value=multimodal>multimodal (vision)</option><option value=embeddings>embeddings</option></select>
<label>tool_call_parser (optional)</label><input id=f_tp placeholder="e.g. qwen3_coder">
<label>reasoning_parser (optional)</label><input id=f_rp placeholder="e.g. qwen3">
<div class=err id=derr></div>
<div class=actions><button class=go style="background:#334155" onclick="dlg.close()">Cancel</button><button class=add onclick=startAdd()>Download → probe → add</button></div>
</dialog>
<dialog id=jdlg><h3>Adding model…</h3><div id=joblog></div>
<div class=actions><button class=go id=jclose onclick="jdlg.close();loadCurrent();loadCached()" disabled>Close</button></div></dialog>
<script>
async function j(u,o){const r=await fetch(u,o);if(r.status==401){location='/login';throw 0;}return r;}
function slug(repo){return repo.split('/').pop().toLowerCase()
  .replace(/-(4bit|8bit|6bit|3bit|mlx|dwq|hf|gguf|instruct|it|chat|unified)\b/g,'')
  .replace(/[^a-z0-9-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,30).replace(/-+$/,'');}
async function search(){
  const q=document.getElementById('q').value.trim(); if(!q)return;
  const res=document.getElementById('results'); res.innerHTML='searching…';
  const d=await(await j('/api/models/search?q='+encodeURIComponent(q))).json();
  if(!d.results.length){res.innerHTML='<div class=sub>no MLX repos found for that query</div>';return;}
  res.innerHTML=''; d.results.forEach(m=>{
    const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div><div class=mono>${m.id}</div><span class=tag>⬇ ${m.downloads}</span> <span class=tag>♥ ${m.likes}</span></div><button class=add>Add</button>`;
    el.querySelector('button').onclick=()=>openAdd(m.id);res.appendChild(el);
  });
}
function openAdd(repo){document.getElementById('derr').textContent='';document.getElementById('f_repo').value=repo;
  document.getElementById('f_role').value=slug(repo);document.getElementById('f_type').value='lm';
  document.getElementById('f_tp').value='';document.getElementById('f_rp').value='';document.getElementById('dlg').showModal();}
async function startAdd(){
  const body={repo:document.getElementById('f_repo').value,role:document.getElementById('f_role').value.trim(),
    type:document.getElementById('f_type').value,tool_parser:document.getElementById('f_tp').value.trim(),
    reasoning_parser:document.getElementById('f_rp').value.trim()};
  const r=await j('/api/models/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); if(!r.ok){document.getElementById('derr').textContent=d.error||'error';return;}
  document.getElementById('dlg').close(); poll(d.job);
}
function poll(jid){
  const box=document.getElementById('joblog'),close=document.getElementById('jclose');
  close.disabled=true; box.textContent=''; document.getElementById('jdlg').showModal();
  const t=setInterval(async()=>{
    const d=await(await j('/api/models/job/'+jid)).json();
    box.innerHTML=`<span class=stage>stage: ${d.stage}</span>\n\n`+(d.log||[]).join('\n');
    box.scrollTop=box.scrollHeight;
    if(d.done){clearInterval(t);close.disabled=false;
      box.innerHTML+='\n\n'+(d.ok?'✅ done — added to the dropdown':'❌ failed — see log above (nothing registered)');}
  },1200);
}
async function loadCached(){
  const d=await(await j('/api/models/cached')).json();
  const box=document.getElementById('cached'); box.innerHTML='';
  const pending=(d.cached||[]).filter(m=>!m.registered);
  if(!pending.length){box.innerHTML='<div class=sub>nothing cached that isn\'t already registered</div>';return;}
  pending.forEach(m=>{const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div class=mono>${m.repo}</div><button class=add>Add</button>`;
    el.querySelector('button').onclick=()=>openAdd(m.repo);box.appendChild(el);});
}
async function loadCurrent(){
  const d=await(await j('/api/models')).json();
  document.getElementById('warn').textContent=d.setup_ok?'':'⚠ installer path not recorded — re-run ./mlx-setup.sh --bootstrap to enable adding models.';
  const cur=document.getElementById('current');cur.innerHTML='';
  (d.served||[]).forEach(s=>{const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div class=mono>${s} <span class=tag>loaded</span></div>`;cur.appendChild(el);});
  (d.custom||[]).forEach(m=>{const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div><div class=mono>${m.role} → ${m.repo}</div><span class=tag>${m.type}</span> <span class=tag>custom</span></div><button class=rm>Remove</button>`;
    el.querySelector('button').onclick=async()=>{if(!confirm('Remove '+m.role+'? (weights kept)'))return;
      await j('/api/models/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:m.role})});loadCurrent();};
    cur.appendChild(el);});
}
loadCurrent();
loadCached();
</script></div></body></html>"""

VISION_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vision · LAW</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<!--NAV_CSS-->
<style>
:root{--bg:#020817;--surf:#0a1628;--card:rgba(13,30,53,.9);--bdr:rgba(14,165,233,.13);--bdrbrt:rgba(14,165,233,.35);
--blue:#0ea5e9;--purple:#7c3aed;--purple2:#a78bfa;--green:#10b981;--red:#ef4444;
--text:#e2e8f0;--muted:#94a3b8;--mono:'JetBrains Mono',ui-monospace,monospace;--sans:'Space Grotesk',system-ui,sans-serif;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
  background-image:radial-gradient(ellipse 70% 50% at 30% -10%,rgba(14,165,233,.08),transparent),
    radial-gradient(ellipse 50% 40% at 80% 0%,rgba(124,58,237,.04),transparent);}
a{color:var(--blue);text-decoration:none}
.vw{display:grid;grid-template-columns:360px 1fr;gap:0;min-height:calc(100vh - 56px)}
@media(max-width:900px){.vw{grid-template-columns:1fr}}
.vc{padding:20px;display:flex;flex-direction:column;gap:14px;
  border-right:1px solid var(--bdr);background:rgba(10,22,40,.6);
  position:sticky;top:56px;max-height:calc(100vh - 56px);overflow-y:auto;
  scrollbar-width:thin;scrollbar-color:rgba(14,165,233,.2) transparent}
@media(max-width:900px){.vc{position:static;max-height:none;border-right:none;border-bottom:1px solid var(--bdr)}}
.sec-lbl{font-family:var(--mono);font-size:10px;letter-spacing:.12em;color:var(--muted);margin-bottom:6px}
.mc{padding:5px 12px;border-radius:20px;border:1px solid var(--bdr);font-size:12px;font-weight:500;
  color:var(--muted);cursor:pointer;transition:.15s;background:transparent;font-family:var(--sans)}
.mc:hover{border-color:var(--blue);color:#7dd3fc}
.mc.active{border-color:var(--blue);background:rgba(14,165,233,.15);color:#7dd3fc}
.drop-zone{border:2px dashed rgba(14,165,233,.2);border-radius:14px;padding:36px 20px;
  text-align:center;cursor:pointer;transition:.25s;position:relative;background:rgba(14,165,233,.02);
  display:flex;flex-direction:column;align-items:center;gap:6px}
.drop-zone:hover,.drop-zone.drag{border-color:var(--blue);background:rgba(14,165,233,.06)}
.drop-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop-icon{font-size:38px;opacity:.35}
.drop-txt{font-size:13px;color:var(--muted)}.drop-hint{font-size:11px;color:rgba(148,163,184,.4)}
.prev-box{position:relative;border-radius:10px;overflow:hidden;border:1px solid var(--bdr)}
.prev-box img{width:100%;max-height:200px;object-fit:contain;display:block;background:var(--surf)}
.prev-rm{position:absolute;top:6px;right:6px;background:rgba(2,8,23,.8);border:1px solid rgba(255,255,255,.15);
  border-radius:6px;color:var(--text);cursor:pointer;font-size:11px;padding:3px 8px;backdrop-filter:blur(4px);font-family:var(--sans)}
.prev-rm:hover{background:rgba(239,68,68,.3)}
.vq{width:100%;min-height:90px;padding:12px;border-radius:10px;
  border:1px solid var(--bdr);background:rgba(13,30,53,.8);color:var(--text);
  font:14px var(--sans);resize:vertical;outline:none;transition:.2s;line-height:1.5}
.vq:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(14,165,233,.12)}
.hint-row{display:flex;gap:5px;flex-wrap:wrap}
.hint{padding:4px 9px;border-radius:12px;font-size:11px;cursor:pointer;border:1px solid var(--bdr);
  color:var(--muted);transition:.15s;background:transparent;font-family:var(--sans)}
.hint:hover{border-color:var(--blue);color:var(--blue);background:rgba(14,165,233,.06)}
.run-btn{width:100%;padding:14px;border:none;border-radius:10px;cursor:pointer;
  font:600 15px var(--sans);color:#fff;transition:.2s;
  background:linear-gradient(135deg,var(--blue),var(--purple))}
.run-btn:not(:disabled):hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(14,165,233,.35)}
.run-btn:disabled{opacity:.55;cursor:default;transform:none}
.prog{height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden;display:none;margin-top:-4px}
.prog.on{display:block}
.pb{height:100%;background:linear-gradient(90deg,var(--blue),var(--purple),var(--blue));background-size:200% 100%;animation:pb 1.4s linear infinite}
@keyframes pb{0%{background-position:0 0}100%{background-position:200% 0}}
.vst{font-size:11px;font-family:var(--mono);color:var(--muted);min-height:14px}
.vst.err{color:var(--red)}.vst.ok{color:var(--green)}
.vr{padding:20px;display:flex;flex-direction:column;gap:20px;min-width:0}
.result-card{background:var(--card);border:1px solid var(--bdr);border-radius:14px;overflow:hidden;backdrop-filter:blur(12px)}
.rc-img{width:100%;max-height:460px;object-fit:contain;display:none;border-bottom:1px solid var(--bdr);background:var(--surf)}
.rc-ph{display:flex;flex-direction:column;align-items:center;gap:14px;padding:80px 20px;color:var(--muted);text-align:center}
.rc-ph .ico{font-size:56px;opacity:.18}
.rc-ph p{font-size:13px;max-width:280px;line-height:1.6}
.rc-body{padding:18px}
.answer-txt{white-space:pre-wrap;font:13px var(--mono);color:var(--text);line-height:1.7;
  max-height:520px;overflow-y:auto;scrollbar-width:thin;scrollbar-color:rgba(14,165,233,.2) transparent}
.rc-acts{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--bdr)}
.rc-btn{padding:7px 14px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid var(--bdr);background:transparent;color:var(--muted);transition:.15s;font-family:var(--sans)}
.rc-btn:hover{border-color:var(--blue);color:var(--blue)}
</style></head><body>
<!--NAV-->
<div class="vw">
  <aside class="vc">
    <div>
      <div class="sec-lbl">MODEL</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="chips">
        <button class="mc active" data-model="mlx-community/Qwen3-VL-8B-Instruct-4bit" data-note="Qwen3-VL-8B · SOTA · ~5.4 GB · fastest" onclick="setModel(this)">Qwen3-VL-8B</button>
        <button class="mc" data-model="mlx-community/gemma-4-12B-it-4bit" data-note="Gemma-4-12B · Google Vision · ~6.7 GB" onclick="setModel(this)">Gemma-4-12B</button>
        <button class="mc" data-model="mlx-community/gemma-4-31B-4bit" data-note="Gemma-4-31B · Google Vision · ~28.8 GB · best" onclick="setModel(this)">Gemma-4-31B</button>
      </div>
      <div class="vst" id="modelNote" style="margin-top:6px">Qwen3-VL-8B · SOTA · ~5.4 GB · fastest</div>
    </div>
    <div>
      <div class="sec-lbl">IMAGE</div>
      <div class="drop-zone" id="dz" ondragover="doDrag(event,true)" ondragleave="doDrag(event,false)" ondrop="doDrop(event)">
        <input type="file" id="imgFile" accept="image/*" onchange="pickFile(this.files[0])">
        <div class="drop-icon">📸</div>
        <div class="drop-txt">Drag &amp; drop or click to upload</div>
        <div class="drop-hint">PNG · JPG · WebP · GIF supported</div>
      </div>
      <div class="prev-box" id="prevBox" style="display:none;margin-top:10px">
        <img id="prevImg">
        <button class="prev-rm" onclick="clearImg()">✕ Remove</button>
      </div>
    </div>
    <div>
      <div class="sec-lbl">QUESTION</div>
      <textarea class="vq" id="prompt" placeholder="Describe this image in detail."></textarea>
      <div class="hint-row" style="margin-top:8px">
        <button class="hint" onclick="setQ('Describe this image in detail.')">Describe</button>
        <button class="hint" onclick="setQ('Extract all text from this image.')">Extract text</button>
        <button class="hint" onclick="setQ('Identify all objects and their positions.')">Objects</button>
        <button class="hint" onclick="setQ('What does this chart or graph show?')">Read chart</button>
        <button class="hint" onclick="setQ('Count all items visible in this image.')">Count items</button>
        <button class="hint" onclick="setQ('Describe the colors, mood, and composition.')">Mood &amp; style</button>
        <button class="hint" onclick="setQ('Is there any code or data in this image? Extract it.')">Code/data</button>
        <button class="hint" onclick="setQ('Describe what is wrong or unusual in this image.')">Anomalies</button>
      </div>
    </div>
    <button class="run-btn" id="runBtn" onclick="analyze()">👁 Analyze Image ›</button>
    <div class="prog" id="prog"><div class="pb"></div></div>
    <div class="vst" id="status"></div>
  </aside>
  <main class="vr">
    <div class="result-card">
      <img class="rc-img" id="rcImg">
      <div class="rc-ph" id="rcPh">
        <div class="ico">👁</div>
        <p>Upload an image and ask a question. Supports OCR, charts, screenshots, photos, documents, and more. All processing is local — no data leaves your machine.</p>
      </div>
      <div class="rc-body" id="rcBody" style="display:none">
        <div class="answer-txt" id="answer"></div>
      </div>
      <div class="rc-acts" id="rcActs" style="display:none">
        <button class="rc-btn" onclick="copyAnswer()">📋 Copy answer</button>
        <button class="rc-btn" onclick="clearResult()">✕ Clear</button>
      </div>
    </div>
  </main>
</div>
<script>
let selModel='mlx-community/Qwen3-VL-8B-Instruct-4bit',imgFile=null;
function setModel(btn){
  selModel=btn.dataset.model;
  document.querySelectorAll('#chips .mc').forEach(b=>b.classList.toggle('active',b===btn));
  document.getElementById('modelNote').textContent=btn.dataset.note||'';
}
function doDrag(e,on){e.preventDefault();document.getElementById('dz').classList.toggle('drag',on);}
function doDrop(e){
  e.preventDefault();document.getElementById('dz').classList.remove('drag');
  const f=[...(e.dataTransfer.files||[])].find(x=>x.type.startsWith('image/'));
  if(f)pickFile(f);
}
function pickFile(f){
  if(!f)return;imgFile=f;
  document.getElementById('dz').style.display='none';
  document.getElementById('prevBox').style.display='block';
  document.getElementById('prevImg').src=URL.createObjectURL(f);
}
function clearImg(){
  imgFile=null;document.getElementById('imgFile').value='';
  document.getElementById('dz').style.display='flex';
  document.getElementById('prevBox').style.display='none';
}
function setQ(q){document.getElementById('prompt').value=q;}
async function analyze(){
  const st=document.getElementById('status');
  if(!imgFile){st.textContent='Upload an image first.';st.className='vst err';return;}
  const btn=document.getElementById('runBtn');
  btn.disabled=true;document.getElementById('prog').classList.add('on');
  st.textContent='Analyzing… (first run loads model — 30–90s)';st.className='vst';
  document.getElementById('rcPh').style.display='none';
  document.getElementById('rcBody').style.display='none';
  document.getElementById('rcActs').style.display='none';
  const rcImg=document.getElementById('rcImg');
  rcImg.src=URL.createObjectURL(imgFile);rcImg.style.display='block';
  const fd=new FormData();fd.append('image',imgFile);
  fd.append('prompt',document.getElementById('prompt').value||'Describe this image in detail.');
  fd.append('model',selModel);
  try{
    const r=await fetch('/api/vision',{method:'POST',body:fd});
    if(r.status===401){location='/login';return;}
    const d=await r.json();
    document.getElementById('answer').textContent=r.ok?d.answer:'⚠ '+(d.error||'failed');
    document.getElementById('rcBody').style.display='block';
    document.getElementById('rcActs').style.display='flex';
    st.textContent=r.ok?'Analysis complete.':'Error.';st.className=r.ok?'vst ok':'vst err';
  }catch(e){
    document.getElementById('rcPh').style.display='flex';
    rcImg.style.display='none';
    st.textContent='⚠ '+e;st.className='vst err';
  }
  btn.disabled=false;document.getElementById('prog').classList.remove('on');
}
function copyAnswer(){
  const t=document.getElementById('answer').textContent;
  navigator.clipboard.writeText(t).catch(()=>{});
  const b=event.target;const p=b.textContent;b.textContent='Copied!';setTimeout(()=>b.textContent=p,2000);
}
function clearResult(){
  document.getElementById('rcImg').style.display='none';
  document.getElementById('rcPh').style.display='flex';
  document.getElementById('rcBody').style.display='none';
  document.getElementById('rcActs').style.display='none';
  document.getElementById('status').textContent='';document.getElementById('status').className='vst';
}
</script></body></html>"""

CHAT_PAGE = r"""<!doctype html><html lang="en"><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>LAW Chat</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<!--NAV_CSS--><style>
:root{--bg:#020817;--surf:#0a1628;--card:rgba(13,30,53,.9);--bdr:rgba(14,165,233,.13);
--blue:#0ea5e9;--purple:#7c3aed;--green:#10b981;--red:#ef4444;
--text:#e2e8f0;--muted:#94a3b8;--mono:'JetBrains Mono',monospace;--sans:'Space Grotesk',system-ui,sans-serif;}
*{box-sizing:border-box;margin:0;padding:0}
html{height:100%}
body{font-family:var(--sans);background:var(--bg);color:var(--text);height:100vh;height:100dvh;
display:flex;flex-direction:column;overflow:hidden;
background-image:radial-gradient(ellipse 60% 40% at 50% -20%,rgba(14,165,233,.06),transparent);}
a{color:var(--blue);text-decoration:none}
.hdr{display:flex;align-items:center;gap:12px;padding:0 16px;height:52px;
border-bottom:1px solid var(--bdr);background:rgba(2,8,23,.85);backdrop-filter:blur(12px);flex-shrink:0}
.hdr-logo{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.06em;color:var(--text)}
.hdr-logo span{color:var(--blue)}
.hdr-sep{color:var(--bdr);margin:0 4px}
select{padding:5px 10px;border-radius:6px;border:1px solid var(--bdr);background:var(--surf);
color:var(--text);font:13px var(--sans);outline:none;cursor:pointer}
select:focus{border-color:var(--blue)}
.mode-toggle{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;
padding:5px 10px;border-radius:6px;border:1px solid var(--bdr);transition:.15s;user-select:none}
.mode-toggle:hover,.mode-toggle.on{background:rgba(14,165,233,.1);border-color:var(--blue);color:var(--blue)}
.clr-btn{margin-left:auto;background:rgba(255,255,255,.04);border:1px solid var(--bdr);border-radius:6px;
padding:5px 10px;color:var(--muted);cursor:pointer;font:12px var(--sans);transition:.15s}
.clr-btn:hover{color:var(--text);border-color:var(--muted)}
.thread{flex:1;overflow-y:auto;min-height:0;padding:20px;display:flex;flex-direction:column;gap:14px;
scrollbar-width:thin;scrollbar-color:rgba(14,165,233,.15) transparent}
.msg-wrap{display:flex;flex-direction:column;animation:fi .25s ease}
.msg-wrap.user{align-items:flex-end}.msg-wrap.ai{align-items:flex-start}
@keyframes fi{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.bubble{max-width:min(680px,88%);padding:12px 16px;border-radius:14px;line-height:1.55;word-break:break-word}
.bubble.user{background:linear-gradient(135deg,rgba(14,165,233,.22),rgba(124,58,237,.18));
border:1px solid rgba(14,165,233,.28);border-bottom-right-radius:4px}
.bubble.ai{background:var(--card);border:1px solid var(--bdr);border-bottom-left-radius:4px;backdrop-filter:blur(8px)}
.bubble img{max-width:240px;border-radius:8px;display:block;margin-bottom:8px}
.think-box{background:rgba(2,8,23,.8);border:1px solid rgba(124,58,237,.2);border-radius:8px;
padding:8px 12px;margin-bottom:10px;font-family:var(--mono);font-size:11px;color:var(--muted);
max-height:180px;overflow-y:auto;white-space:pre-wrap;line-height:1.5}
details.steps summary{cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--purple);
list-style:none;padding:2px 0;margin-bottom:6px}
details.steps summary::-webkit-details-marker{display:none}
.bubble.ai h2{font-size:17px;margin:.5em 0 .3em}
.bubble.ai h3{font-size:15px;margin:.5em 0 .3em}
.bubble.ai h4{font-size:14px;color:#c4b5fd;margin:.5em 0 .3em}
.bubble.ai p{margin:.4em 0}.bubble.ai ul,.bubble.ai ol{margin:.4em 0;padding-left:1.4em}
.bubble.ai li{margin:.15em 0}.bubble.ai a{color:var(--blue)}
.bubble.ai strong{color:#f1f5f9}
.bubble.ai blockquote{border-left:3px solid var(--blue);margin:.5em 0;padding:.2em 0 .2em .8em;color:var(--muted)}
.bubble.ai pre{background:rgba(2,8,23,.9);border:1px solid rgba(14,165,233,.15);border-radius:8px;
padding:12px;overflow:auto;margin:.5em 0;font-family:var(--mono);font-size:12px}
.bubble.ai code{background:rgba(2,8,23,.6);border:1px solid rgba(14,165,233,.12);
border-radius:4px;padding:1px 5px;font-family:var(--mono);font-size:12px;color:#7dd3fc}
.bubble.ai pre code{background:none;border:0;padding:0;color:#e2e8f0}
.typing{display:flex;gap:4px;align-items:center;padding:4px 0}
.typing span{width:6px;height:6px;border-radius:50%;background:var(--blue);animation:blink 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}.typing span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}
.img-chip{display:none;align-items:center;gap:8px;padding:6px 10px;border-radius:8px;
border:1px solid rgba(124,58,237,.3);background:rgba(124,58,237,.08);font-size:12px;margin-bottom:6px}
.img-chip img{height:32px;border-radius:5px}
.img-chip .rm{cursor:pointer;color:var(--red);font-weight:700;font-size:14px;padding:0 4px}
.foot{padding:12px 16px;border-top:1px solid var(--bdr);background:rgba(2,8,23,.8);backdrop-filter:blur(12px);flex-shrink:0}
.inp-row{display:flex;gap:8px;align-items:flex-end}
@media(max-width:640px){
  .hdr{height:44px;padding:0 10px;flex-wrap:nowrap;gap:6px;overflow:hidden}
  .hdr-back,.hdr-logo,.hdr-sep,.note{display:none}
  #model{flex:1;min-width:0;max-width:none!important;font-size:12px;padding:4px 8px}
  .mode-toggle{padding:4px 8px;font-size:11px;flex-shrink:0}
  .clr-btn{padding:4px 8px;font-size:11px;flex-shrink:0;margin-left:0}
  .thread{padding:10px 8px}
  .bubble{max-width:94%;font-size:14px}
  .foot{padding:8px 10px}
  #in{font-size:16px}
}
.att-btn{width:38px;height:38px;border:1px solid var(--bdr);border-radius:8px;background:var(--surf);
color:var(--muted);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:.15s}
.att-btn:hover{border-color:var(--purple);color:var(--purple)}
#in{flex:1;padding:10px 14px;border-radius:10px;border:1px solid var(--bdr);background:var(--surf);
color:var(--text);font:14px var(--sans);resize:none;outline:none;transition:.2s;min-height:38px;max-height:160px}
#in:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(14,165,233,.1)}
.send-btn{width:38px;height:38px;border:none;border-radius:8px;cursor:pointer;
background:linear-gradient(135deg,var(--blue),var(--purple));color:#fff;font-size:18px;
display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:.2s}
.send-btn:hover{opacity:.9;transform:translateY(-1px)}
.send-btn:disabled{opacity:.4;cursor:default;transform:none}
.note{font-family:var(--mono);font-size:10px;color:var(--muted);padding:4px 0 0}
body.drag{outline:3px dashed var(--purple);outline-offset:-4px}
.clr-btn.voice-on{background:rgba(16,185,129,.15);border-color:var(--green);color:var(--green)}
.voice-player{display:none}
.ba-btn{font-size:11px;padding:3px 10px;border-radius:5px;cursor:pointer;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.04);color:var(--muted);margin-top:6px;margin-right:4px;transition:.15s;font-family:var(--sans)}
.ba-btn:hover{background:rgba(255,255,255,.1);color:var(--text)}
.ba-btn.del{border-color:rgba(239,68,68,.3);color:#ef4444;background:rgba(239,68,68,.06)}
.ba-btn.del:hover{background:rgba(239,68,68,.18)}
.ba-btn.speak{float:right}
</style></head><body>
<!--NAV-->
<div class="hdr">
<a class="hdr-back" href="/">&#8592;</a>
<span class="hdr-logo"><span>LAW</span> CHAT</span>
<span class="hdr-sep">|</span>
<div class="mode-toggle on" id="modeBtn" onclick="toggleMode()">&#127760; Agent</div>
<select id=model style="max-width:200px"></select>
<span class="note" id="note"></span>
<button class="clr-btn" id="voiceBtn" onclick="toggleVoiceMode()" title="Voice mode: auto-speak agent responses">&#127908; Voice</button>
<button class="clr-btn" onclick="msgs=[];render()">Clear</button>
</div>
<div class="thread" id=thread></div>
<div class="foot">
<div class="img-chip" id=chip><img id=chipimg><span id=chipname></span><span class="rm" onclick="clearImg()">&#10005;</span></div>
<div class="inp-row">
<input type=file id=file accept="image/*" style="display:none" onchange="pickImg(this.files[0])">
<button class="att-btn" onclick="document.getElementById('file').click()" title="Attach image">&#128206;</button>
<textarea id=in rows=1 placeholder="Message&#x2026; (Enter to send, Shift+Enter for newline)" onkeydown="key(event)" oninput="autosize(this)"></textarea>
<button class="send-btn" id=sendbtn onclick=send()>&#8593;</button>
</div>
<div class="note" id="note2"></div>
</div>
<script>
let msgs=[],busy=false,img=null,agentOn=true;
const CHAT_KEY='law_chat_v2';
const IMG_TRIGGER=/^(generate|create|make|draw|show me|give me|produce)\s+(a |an |some )?(image|photo|picture|illustration|art|drawing|painting|render|visualization) of (.+)/i;
function autosize(ta){ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,160)+'px';}
async function j(u,o){const r=await fetch(u,o);if(r.status==401){location='/login';throw 0;}return r;}
function saveChatState(){
  try{
    const savable=msgs.filter(m=>m.content&&m.content!=='__typing__').slice(-60).map(m=>{
      const c={...m};if(c.image&&c.image.startsWith('blob:'))delete c.image;return c;});
    sessionStorage.setItem(CHAT_KEY,JSON.stringify({msgs:savable,agentOn}));
  }catch(e){}
}
function restoreChatState(){
  try{
    const saved=JSON.parse(sessionStorage.getItem(CHAT_KEY)||'{}');
    if(saved.msgs&&saved.msgs.length){
      msgs=saved.msgs;
      if(saved.agentOn!==undefined&&saved.agentOn!==agentOn){
        agentOn=saved.agentOn;
        const b=document.getElementById('modeBtn');
        b.classList.toggle('on',agentOn);b.innerHTML=agentOn?'&#127760; Agent':'&#9889; Direct';
      }
      render();
    }
  }catch(e){}
}
function toggleMode(){agentOn=!agentOn;const b=document.getElementById('modeBtn');
b.classList.toggle('on',agentOn);b.innerHTML=(agentOn?'&#127760; Agent':'&#9889; Direct');loadDropdown();}
async function loadDropdown(){
const sel=document.getElementById('model'),nt=document.getElementById('note');
if(agentOn){
const d=await(await j('/api/personas')).json();
const names=(d.personas||[]).map(p=>p.name);
sel.innerHTML=names.map(n=>`<option>${n}</option>`).join('')||'<option>orchestrator</option>';
if(names.includes('orchestrator'))sel.value='orchestrator';
nt.textContent='agent mode: tools, web search, file access';
}else{
const d=await(await j('/api/models')).json();
const list=(d.gateway||[]).filter(x=>!x.startsWith('embed'));
sel.innerHTML=list.map(x=>`<option>${x}</option>`).join('')||'<option>no models</option>';
nt.textContent=list.length?'direct: fast, no tools':'gateway offline';
}
}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmt(raw){
raw=(raw||'').replace(/\r/g,'');
const fence=[],ic=[];
raw=raw.replace(/```(?:[\w-]*)\n?([\s\S]*?)```/g,(m,c)=>{fence.push(c.replace(/\n$/,''));return '\u0000F'+(fence.length-1)+'\u0000';});
raw=raw.replace(/`([^`\n]+)`/g,(m,c)=>{ic.push(c);return '\u0000C'+(ic.length-1)+'\u0000';});
const rc=s=>s.replace(/\u0000C(\d+)\u0000/g,(m,n)=>'<code>'+esc(ic[+n])+'</code>');
const inl=s=>{s=esc(s);
s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target=_blank rel=noopener>$1</a>');
return rc(s);};
const L=raw.split('\n'),out=[];let i=0;
const isBlk=s=>/^(#{1,4})\s|^\s*[-*+]\s|^\s*\d+\.\s|^\u0000F\d+\u0000\s*$|^>\s?/.test(s);
while(i<L.length){
let ln=L[i];
const f=ln.trim().match(/^\u0000F(\d+)\u0000$/);
if(f){out.push('<pre><code>'+esc(fence[+f[1]])+'</code></pre>');i++;continue;}
const h=ln.match(/^(#{1,4})\s+(.*)$/);
if(h){const lv=Math.min(h[1].length+1,6);out.push('<h'+lv+'>'+inl(h[2])+'</h'+lv+'>');i++;continue;}
if(/^\s*[-*+]\s+/.test(ln)){let it=[];while(i<L.length&&/^\s*[-*+]\s+/.test(L[i])){it.push('<li>'+inl(L[i].replace(/^\s*[-*+]\s+/,''))+'</li>');i++;}out.push('<ul>'+it.join('')+'</ul>');continue;}
if(/^\s*\d+\.\s+/.test(ln)){let it=[];while(i<L.length&&/^\s*\d+\.\s+/.test(L[i])){it.push('<li>'+inl(L[i].replace(/^\s*\d+\.\s+/,''))+'</li>');i++;}out.push('<ol>'+it.join('')+'</ol>');continue;}
if(/^>\s?/.test(ln)){let q=[];while(i<L.length&&/^>\s?/.test(L[i])){q.push(inl(L[i].replace(/^>\s?/,'')));i++;}out.push('<blockquote>'+q.join('<br>')+'</blockquote>');continue;}
if(ln.trim()===''){i++;continue;}
let p=[];while(i<L.length&&L[i].trim()!==''&&!isBlk(L[i])){p.push(inl(L[i]));i++;}
out.push('<p>'+p.join('<br>')+'</p>');
}
return out.join('');
}
function makeBubble(m){
const wrap=document.createElement('div');wrap.className='msg-wrap '+(m.role==='user'?'user':'ai');
const b=document.createElement('div');b.className='bubble '+(m.role==='user'?'user':'ai');
let h='';
if(m.image)h='<img src="'+m.image+'">';
if(m.role==='user'){h+=esc(m.content||'');}
else{
if(m.steps&&m.steps.length){
h+='<details class=steps><summary>&#9654; Thinking ('+m.steps.length+' steps)</summary><div class=think-box>'+esc(m.steps.join('\n'))+'</div></details>';
}
if(m.content==='__typing__'){h+='<div class=typing><span></span><span></span><span></span></div>';}
else{h+=fmt(m.content||'');}
if(m.imageResult){
  const fname=(m.imageResult.match(/\/api\/image-file\/([^?]+)/)||[])[1]||'';
  h+='<img src="'+m.imageResult+'" style="max-width:100%;max-height:380px;border-radius:10px;margin-top:10px;display:block;border:1px solid rgba(14,165,233,.2);cursor:pointer" onclick="window.open(this.src)">';
  if(fname)h+='<button class="ba-btn del" onclick="delChatImg(this,\''+fname+'\')">🗑 Delete from disk</button>';
}
if(m.content&&m.content!=='__typing__'){
  const enc=encodeURIComponent(m.content);
  h+='<button class="ba-btn speak" data-speak="'+enc+'" onclick="speakTxt(this)">🔊</button>';
}
}
b.innerHTML=h;wrap.appendChild(b);return wrap;
}
function render(){const t=document.getElementById('thread');t.innerHTML='';
msgs.forEach(m=>t.appendChild(makeBubble(m)));t.scrollTop=t.scrollHeight;saveChatState();return t;}
function key(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}}
function pickImg(f){if(!f)return;img=f;
document.getElementById('chip').style.display='flex';
document.getElementById('chipimg').src=URL.createObjectURL(f);
document.getElementById('chipname').textContent=f.name.slice(0,24);}
function clearImg(){img=null;document.getElementById('file').value='';document.getElementById('chip').style.display='none';}
document.addEventListener('dragover',e=>{e.preventDefault();document.body.classList.add('drag');});
document.addEventListener('dragleave',e=>{if(!e.relatedTarget)document.body.classList.remove('drag');});
document.addEventListener('drop',e=>{e.preventDefault();document.body.classList.remove('drag');
const f=[...(e.dataTransfer.files||[])].find(x=>x.type.startsWith('image/'));if(f)pickImg(f);});
async function send(){
if(busy)return;
const inp=document.getElementById('in'),text=inp.value.trim();
if(!text&&!img)return;
busy=true;document.getElementById('sendbtn').disabled=true;
const imgM=!img&&text.match(IMG_TRIGGER);
if(imgM)await imageGenTurn(text,imgM[4]||text.replace(IMG_TRIGGER,'').trim());
else if(img)await visionTurn(text);
else if(agentOn)await agentTurn(text);
else await chatTurn(text);
busy=false;document.getElementById('sendbtn').disabled=false;
}
async function imageGenTurn(text,subject){
const inp=document.getElementById('in');
inp.value='';autosize(inp);
msgs.push({role:'user',content:text});
const a={role:'assistant',content:'__typing__',steps:['🎨 Generating image from prompt…']};msgs.push(a);
const t=render();
function paint(){const prev=t.lastChild;prev.replaceWith(makeBubble(a));t.scrollTop=t.scrollHeight;}
try{
const r=await j('/api/imagine',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({prompt:subject,model:'qwen',steps:20,width:1024,height:1024})});
const d=await r.json();
if(r.ok){
  const url='/api/image-file/'+d.file+'?t='+Date.now();
  a.content='Here\'s your generated image:';a.imageResult=url;a.steps=[];
}else{
  a.content='⚠ Image generation failed: '+(d.error||'failed');a.steps=[];
}
}catch(e){a.content='Error: '+e;a.steps=[];}
paint();saveChatState();
}
async function agentTurn(text){
const persona=document.getElementById('model').value;
document.getElementById('in').value='';autosize(document.getElementById('in'));
const hist=msgs.filter(x=>x.content&&x.content!=='__typing__'&&(x.role==='user'||x.role==='assistant'))
.map(x=>({role:x.role,content:x.content}));
msgs.push({role:'user',content:text});
const a={role:'assistant',content:'__typing__',steps:[]};msgs.push(a);
const t=render();
function paint(){const prev=t.lastChild;prev.replaceWith(makeBubble(a));t.scrollTop=t.scrollHeight;}
try{
const r=await j('/api/agent-chat',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({persona,message:text,history:hist})});
if(r.status===429){a.content='Agent busy — try again in a moment';a.steps=[];paint();return;}
const reader=r.body.getReader(),dec=new TextDecoder();let buf='';a.steps=[];
while(true){
const{done,value}=await reader.read();if(done)break;
buf+=dec.decode(value,{stream:true});let i;
while((i=buf.indexOf('\n'))>=0){
const ln=buf.slice(0,i);buf=buf.slice(i+1);if(!ln.trim())continue;
let o;try{o=JSON.parse(ln);}catch(e){continue;}
if(o.type==='trace'){a.steps.push(o.text);a.content='__typing__';}
else if(o.type==='answer'){a.content=o.text;}
paint();
}
}
if(a.content==='__typing__'||!a.content){a.content='[no answer]';}paint();saveChatState();autoSpeak(a.content);
}catch(e){a.content='Error: '+e;a.steps=[];paint();saveChatState();}
}
async function chatTurn(text){
const model=document.getElementById('model').value;
document.getElementById('in').value='';autosize(document.getElementById('in'));
msgs.push({role:'user',content:text});
const a={role:'assistant',content:'__typing__'};msgs.push(a);
const t=render();
try{
const r=await j('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({model,messages:msgs.filter(m=>m.content&&m.content!=='__typing__').slice(0,-1)})});
const reader=r.body.getReader(),dec=new TextDecoder();let acc='';
while(true){
const{done,value}=await reader.read();if(done)break;
acc+=dec.decode(value,{stream:true});a.content=acc;
const nb=makeBubble(a);t.lastChild.replaceWith(nb);t.scrollTop=t.scrollHeight;
}
if(!acc){a.content='[no output]';t.lastChild.replaceWith(makeBubble(a));}
saveChatState();autoSpeak(a.content);
}catch(e){a.content='Error: '+e;t.lastChild.replaceWith(makeBubble(a));saveChatState();}
}
async function visionTurn(text){
const f=img,prompt=text||'Describe this image in detail.';
const url=URL.createObjectURL(f);document.getElementById('in').value='';clearImg();
autosize(document.getElementById('in'));
msgs.push({role:'user',content:prompt,image:url});
const a={role:'assistant',content:'__typing__'};msgs.push(a);
const t=render();
try{
const fd=new FormData();fd.append('image',f);fd.append('prompt',prompt);
const r=await j('/api/vision',{method:'POST',body:fd}),d=await r.json();
a.content=r.ok?d.answer:'Error: '+(d.error||'failed');
}catch(e){a.content='Error: '+e;}
t.lastChild.replaceWith(makeBubble(a));t.scrollTop=t.scrollHeight;saveChatState();autoSpeak(a.content);
}
// ── Voice Mode (auto-TTS agent responses via Kokoro backend) ─────────────────
let voiceMode=false;
const voiceAudio=new Audio();
let _ttsCtrl=null; // single AbortController for all TTS fetches — cancels stale requests
function _cancelTTS(){if(_ttsCtrl){_ttsCtrl.abort();_ttsCtrl=null;}voiceAudio.pause();voiceAudio.src='';}
function toggleVoiceMode(){
  voiceMode=!voiceMode;
  const btn=document.getElementById('voiceBtn');
  btn.classList.toggle('voice-on',voiceMode);
  btn.innerHTML=voiceMode?'&#127908; Voice ON':'&#127908; Voice';
  if(!voiceMode){_cancelTTS();}
}
async function autoSpeak(text){
  if(!voiceMode||!text||text==='__typing__')return;
  const clean=text.replace(/```[\s\S]*?```/g,'code block').replace(/`[^`]+`/g,'').replace(/[*_#>]/g,'').trim();
  if(!clean)return;
  _cancelTTS();
  _ttsCtrl=new AbortController();
  const sig=_ttsCtrl.signal;
  try{
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:clean.slice(0,3000),voice:'af_heart',speed:1.0}),signal:sig});
    if(!r.ok||sig.aborted)return;
    const d=await r.json();
    if(!d.file||sig.aborted)return;
    voiceAudio.src='/api/audio-file/'+d.file+'?t='+Date.now();
    voiceAudio.play().catch(()=>{});
  }catch(e){}
}
async function speakTxt(btn){
  _cancelTTS();
  speechSynthesis.cancel();
  document.querySelectorAll('.ba-btn.speak').forEach(b=>{b.textContent='🔊';});
  if(btn.textContent.includes('Stop'))return;
  const raw=decodeURIComponent(btn.dataset.speak||'').trim();
  if(!raw)return;
  const clean=raw.replace(/```[\s\S]*?```/g,'code block').replace(/`[^`]+`/g,'').replace(/[*_#>\[\]]/g,'').trim();
  if(!clean)return;
  _ttsCtrl=new AbortController();
  const sig=_ttsCtrl.signal;
  try{
    btn.textContent='⏳';
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:clean.slice(0,3000),voice:'af_heart',speed:1.0}),signal:sig});
    if(!r.ok||sig.aborted){btn.textContent='🔊';return;}
    const d=await r.json();
    if(!d.file||sig.aborted){btn.textContent='🔊';return;}
    btn.textContent='⏹ Stop';
    voiceAudio.src='/api/audio-file/'+d.file+'?t='+Date.now();
    voiceAudio.onended=()=>{btn.textContent='🔊';_ttsCtrl=null;};
    voiceAudio.play().catch(()=>{btn.textContent='🔊';});
  }catch(e){if(e.name!=='AbortError')btn.textContent='🔊';}
}
async function delChatImg(btn,fname){
  if(!confirm('Delete '+fname+' from this machine?'))return;
  const r=await j('/api/delete-file',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:fname,kind:'image'})});
  if(r.ok){
    const idx=msgs.findIndex(m=>m.imageResult&&m.imageResult.includes(fname));
    if(idx>=0)msgs.splice(idx,1);
    render();
  }else{try{const d=await r.json();alert('Delete failed: '+(d.error||'unknown'));}catch(e){alert('Delete failed');}}
}
(async()=>{await loadDropdown();restoreChatState();})();
// P6-2: mic input (browser SpeechRecognition)
const SpeechRec=window.SpeechRecognition||window.webkitSpeechRecognition;
if(SpeechRec){
  const micBtn=document.createElement('button');
  micBtn.className='att-btn';micBtn.title='Voice input';micBtn.innerHTML='🎙';
  micBtn.style.cssText='';
  let rec=null,recording=false,_micLastT='';
  micBtn.onclick=function(){
    if(recording){rec&&rec.stop();recording=false;micBtn.innerHTML='🎙';micBtn.style.borderColor='';return;}
    rec=new SpeechRec();rec.lang='en-US';rec.interimResults=false;rec.continuous=false;
    _micLastT='';
    rec.onresult=e=>{
      let t='';
      for(let i=e.resultIndex;i<e.results.length;i++){if(e.results[i].isFinal)t+=e.results[i][0].transcript;}
      t=t.trim();
      if(!t||t===_micLastT)return;   // guard against webkit double-fire
      _micLastT=t;
      const inp=document.getElementById('in');
      inp.value=(inp.value?(inp.value+' '+t):t).trim();
      autosize(inp);
    };
    rec.onend=()=>{recording=false;micBtn.innerHTML='🎙';micBtn.style.borderColor='';_micLastT='';};
    rec.start();recording=true;micBtn.innerHTML='🔴';micBtn.style.borderColor='#ef4444';
  };
  document.querySelector('.inp-row').insertBefore(micBtn,document.getElementById('in'));
}
</script></body></html>"""


IMAGINE_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Create · LAW</title>
<!--NAV_CSS-->
<style>
:root{--bg:#020817;--surf:#0a1628;--card:rgba(13,30,53,.9);--bdr:rgba(14,165,233,.13);--bdrbrt:rgba(14,165,233,.35);
--purple:#7c3aed;--purple2:#a78bfa;--blue:#0ea5e9;--green:#10b981;--red:#ef4444;--amber:#f59e0b;
--text:#e2e8f0;--muted:#94a3b8;--mono:'JetBrains Mono',ui-monospace,monospace;--sans:'Space Grotesk',system-ui,sans-serif;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
  background-image:radial-gradient(ellipse 70% 50% at 30% -10%,rgba(124,58,237,.1),transparent),
    radial-gradient(ellipse 50% 40% at 80% 0%,rgba(14,165,233,.06),transparent);}
a{color:var(--blue);text-decoration:none}
/* ── page layout ── */
.ig-wrap{display:grid;grid-template-columns:380px 1fr;gap:0;min-height:calc(100vh - 56px)}
@media(max-width:900px){.ig-wrap{grid-template-columns:1fr;grid-template-rows:auto 1fr}}
/* ── left control panel ── */
.ig-controls{padding:20px;display:flex;flex-direction:column;gap:14px;
  border-right:1px solid var(--bdr);background:rgba(10,22,40,.6);
  position:sticky;top:56px;max-height:calc(100vh - 56px);overflow-y:auto;
  scrollbar-width:thin;scrollbar-color:rgba(14,165,233,.2) transparent}
@media(max-width:900px){.ig-controls{position:static;max-height:none;border-right:none;border-bottom:1px solid var(--bdr)}}
.sec-lbl{font-family:var(--mono);font-size:10px;letter-spacing:.12em;color:var(--muted);margin-bottom:6px}
/* model chips */
.model-chips{display:flex;gap:6px;flex-wrap:wrap}
.mc{padding:5px 12px;border-radius:20px;border:1px solid var(--bdr);font-size:12px;font-weight:500;
  color:var(--muted);cursor:pointer;transition:.15s;background:transparent;font-family:var(--sans)}
.mc:hover{border-color:var(--purple);color:var(--purple2)}
.mc.active{border-color:var(--purple);background:rgba(124,58,237,.15);color:var(--purple2)}
/* prompt */
.ig-prompt{width:100%;min-height:120px;padding:12px;border-radius:10px;
  border:1px solid var(--bdr);background:rgba(13,30,53,.8);color:var(--text);
  font:14px var(--sans);resize:vertical;outline:none;transition:.2s;line-height:1.5}
.ig-prompt:focus{border-color:var(--purple);box-shadow:0 0 0 2px rgba(124,58,237,.12)}
/* style presets */
.presets{display:flex;gap:5px;flex-wrap:wrap}
.preset{padding:4px 10px;border-radius:14px;font-size:11px;cursor:pointer;border:1px solid var(--bdr);
  color:var(--muted);transition:.15s;background:transparent;font-family:var(--sans)}
.preset:hover{border-color:var(--blue);color:var(--blue);background:rgba(14,165,233,.06)}
/* resolution presets */
.res-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.res-btn{display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 4px;
  border-radius:8px;border:1px solid var(--bdr);cursor:pointer;color:var(--muted);
  background:transparent;transition:.15s;font-family:var(--sans)}
.res-btn:hover{border-color:var(--blue);color:var(--blue)}
.res-btn.active{border-color:var(--purple);background:rgba(124,58,237,.12);color:var(--purple2)}
.res-icon{display:flex;align-items:flex-end;justify-content:center;height:28px}
.res-icon div{background:currentColor;border-radius:2px;opacity:.8}
.res-lbl{font-size:10px;font-family:var(--mono)}
/* steps slider */
.slider-wrap{display:flex;align-items:center;gap:10px}
.slider-wrap input[type=range]{flex:1;-webkit-appearance:none;height:4px;border-radius:2px;
  background:linear-gradient(90deg,var(--purple) var(--pct,50%),rgba(255,255,255,.1) var(--pct,50%));
  outline:none;cursor:pointer}
.slider-wrap input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--purple);box-shadow:0 0 6px rgba(124,58,237,.5)}
.slider-val{font-family:var(--mono);font-size:13px;color:var(--purple2);min-width:22px;text-align:right}
/* seed row */
.seed-row{display:flex;gap:8px}
.seed-inp{flex:1;padding:8px 10px;border-radius:8px;border:1px solid var(--bdr);
  background:rgba(13,30,53,.8);color:var(--text);font:13px var(--mono);outline:none;transition:.2s}
.seed-inp:focus{border-color:var(--blue)}
.seed-btn{padding:8px 10px;border-radius:8px;border:1px solid var(--bdr);background:transparent;
  color:var(--muted);cursor:pointer;font-size:13px;transition:.15s;font-family:var(--sans)}
.seed-btn:hover{border-color:var(--blue);color:var(--blue)}
/* neg prompt */
.adv-toggle{font-size:12px;color:var(--muted);cursor:pointer;display:flex;align-items:center;gap:5px;
  padding:4px 0;border:none;background:transparent;font-family:var(--sans)}
.adv-toggle:hover{color:var(--text)}.adv-arrow{transition:.2s}.adv-arrow.open{transform:rotate(90deg)}
.adv-panel{display:none;flex-direction:column;gap:8px;padding-top:6px}
.adv-panel.open{display:flex}
.neg-inp{width:100%;min-height:60px;padding:9px;border-radius:8px;border:1px solid var(--bdr);
  background:rgba(13,30,53,.8);color:var(--text);font:13px var(--sans);resize:vertical;outline:none;transition:.2s}
.neg-inp:focus{border-color:var(--red)}
/* lic note */
.lic-note{font-size:11px;color:var(--muted);font-family:var(--mono)}
/* mode tabs */
.mode-tabs{display:flex;border-radius:8px;overflow:hidden;border:1px solid var(--bdr);margin-bottom:4px}
.mode-tab{flex:1;padding:9px;border:none;cursor:pointer;font:600 12px var(--sans);
  background:transparent;color:var(--muted);transition:.15s}
.mode-tab.active{background:rgba(124,58,237,.2);color:var(--purple2)}
.mode-tab:hover:not(.active){background:rgba(255,255,255,.04);color:var(--text)}
/* edit source drop */
.edit-dz{border:2px dashed rgba(14,165,233,.2);border-radius:10px;padding:18px;
  text-align:center;cursor:pointer;transition:.2s;font-size:12px;color:var(--muted);position:relative}
.edit-dz:hover{border-color:var(--blue);background:rgba(14,165,233,.04)}
.edit-dz input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.edit-prev{max-width:100%;max-height:140px;border-radius:8px;display:none;margin-top:8px;border:1px solid var(--bdr)}
.edit-chips{display:flex;gap:6px}
.ec{padding:5px 12px;border-radius:14px;border:1px solid var(--bdr);font-size:12px;
  color:var(--muted);cursor:pointer;transition:.15s;background:transparent;font-family:var(--sans)}
.ec.active{border-color:var(--purple);background:rgba(124,58,237,.15);color:var(--purple2)}
.ec:hover{border-color:var(--blue);color:#7dd3fc}
/* generate button */
.gen-btn{width:100%;padding:14px;border:none;border-radius:10px;cursor:pointer;
  font:600 15px var(--sans);color:#fff;position:relative;overflow:hidden;transition:.2s;margin-top:4px;
  background:linear-gradient(135deg,var(--purple),#6d28d9)}
.gen-btn:not(:disabled):hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(124,58,237,.4)}
.gen-btn:disabled{opacity:.6;cursor:default;transform:none}
.gen-btn .btn-txt{position:relative;z-index:1}
.gen-btn .btn-glow{position:absolute;inset:0;background:linear-gradient(135deg,rgba(167,139,250,.3),transparent);
  opacity:0;transition:.3s}
.gen-btn:not(:disabled):hover .btn-glow{opacity:1}
/* progress bar */
.progress-bar{height:3px;width:100%;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden;
  display:none;margin-top:-4px}
.progress-bar.active{display:block}
.pb-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--purple),var(--blue),var(--purple));
  background-size:200% 100%;animation:pbAnim 1.5s linear infinite;width:100%}
@keyframes pbAnim{0%{background-position:0 0}100%{background-position:200% 0}}
/* ── right panel ── */
.ig-right{padding:20px;display:flex;flex-direction:column;gap:20px;min-width:0}
/* result area */
.result-area{border-radius:14px;border:1px solid var(--bdr);background:var(--card);
  overflow:hidden;position:relative;min-height:200px;display:flex;align-items:center;justify-content:center}
.result-placeholder{display:flex;flex-direction:column;align-items:center;gap:12px;padding:60px 20px;
  color:var(--muted);text-align:center}
.result-placeholder .big{font-size:48px;opacity:.3}
.result-placeholder p{font-size:13px;max-width:280px;line-height:1.5}
.result-img{width:100%;display:block;border-radius:14px}
.result-overlay{position:absolute;bottom:0;left:0;right:0;padding:12px 16px;
  background:linear-gradient(transparent,rgba(2,8,23,.9));
  display:flex;align-items:center;gap:8px;opacity:0;transition:.2s}
.result-area:hover .result-overlay{opacity:1}
.res-act{padding:6px 12px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid rgba(255,255,255,.15);background:rgba(2,8,23,.7);color:var(--text);
  backdrop-filter:blur(8px);transition:.15s;font-family:var(--sans)}
.res-act:hover{background:rgba(255,255,255,.1)}
.res-stats{font-family:var(--mono);font-size:10px;color:var(--muted);margin-left:auto}
/* status / error */
.ig-status{font-size:13px;color:var(--muted);min-height:18px;font-family:var(--mono)}
.ig-status.error{color:var(--red)}
.ig-status.ok{color:var(--green)}
/* gallery */
.gallery-header{display:flex;align-items:center;gap:10px}
.gallery-ttl{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--muted)}
.gallery-count{font-family:var(--mono);font-size:10px;color:var(--muted);background:rgba(255,255,255,.06);
  padding:2px 7px;border-radius:10px}
.gallery-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
@media(max-width:600px){.gallery-grid{grid-template-columns:repeat(auto-fill,minmax(100px,1fr))}}
.gal-item{position:relative;border-radius:8px;overflow:hidden;cursor:pointer;
  border:1px solid var(--bdr);aspect-ratio:1;background:var(--surf);transition:.2s}
.gal-item:hover{border-color:var(--bdrbrt);transform:scale(1.02);z-index:2}
.gal-item img{width:100%;height:100%;object-fit:cover;display:block}
.gal-item .gal-overlay{position:absolute;inset:0;background:linear-gradient(transparent 40%,rgba(2,8,23,.92));
  opacity:0;transition:.2s;display:flex;flex-direction:column;justify-content:flex-end;padding:8px}
.gal-item:hover .gal-overlay{opacity:1}
.gal-prompt{font-size:10px;color:var(--text);line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.gal-dl{position:absolute;top:6px;right:6px;width:24px;height:24px;border-radius:5px;
  background:rgba(2,8,23,.7);color:var(--text);display:flex;align-items:center;justify-content:center;
  font-size:12px;text-decoration:none;opacity:0;transition:.2s;backdrop-filter:blur(4px)}
.gal-del{position:absolute;top:6px;left:6px;width:24px;height:24px;border-radius:5px;border:none;
  background:rgba(239,68,68,.75);color:#fff;display:flex;align-items:center;justify-content:center;
  font-size:11px;cursor:pointer;opacity:0;transition:.2s;backdrop-filter:blur(4px)}
.gal-item:hover .gal-dl,.gal-item:hover .gal-del{opacity:1}
/* lightbox */
.lb{display:none;position:fixed;inset:0;z-index:999;background:rgba(2,8,23,.92);
  backdrop-filter:blur(12px);align-items:center;justify-content:center;padding:20px}
.lb.open{display:flex}
.lb-inner{position:relative;max-width:min(90vw,1200px);max-height:90vh}
.lb-img{max-width:100%;max-height:90vh;border-radius:12px;display:block;
  box-shadow:0 24px 80px rgba(0,0,0,.6)}
.lb-close{position:absolute;top:-12px;right:-12px;width:32px;height:32px;border-radius:50%;
  background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:var(--text);
  cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:16px;
  transition:.15s;font-family:var(--sans)}
.lb-close:hover{background:rgba(255,255,255,.2)}
.lb-actions{position:absolute;bottom:-44px;left:0;right:0;display:flex;justify-content:center;gap:8px}
.lb-btn{padding:6px 16px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid rgba(255,255,255,.15);background:rgba(10,22,40,.8);color:var(--text);
  backdrop-filter:blur(8px);font-family:var(--sans)}
.lb-btn:hover{background:rgba(255,255,255,.1)}
</style></head><body>
<!--NAV-->
<div class="ig-wrap">
  <!-- LEFT: Controls -->
  <aside class="ig-controls">
    <div class="mode-tabs">
      <button class="mode-tab active" id="tab-gen" onclick="setMode('gen')">✦ Generate</button>
      <button class="mode-tab" id="tab-edit" onclick="setMode('edit')">✎ Edit Image</button>
    </div>
    <div id="gen-section">
      <div class="sec-lbl">MODEL</div>
      <div class="model-chips">
        <button class="mc active" data-model="qwen" onclick="setModel(this)">Qwen-Image 20B</button>
        <button class="mc" data-model="ernie" onclick="setModel(this)">ERNIE-Image 8B</button>
        <button class="mc" data-model="z-image-turbo" onclick="setModel(this)">Z-Image Turbo</button>
      </div>
      <div class="lic-note" id="licNote" style="margin-top:6px">Qwen/Qwen-Image 20.4B · 4-bit · Apache-2.0 · best quality</div>
    </div>
    <div id="edit-section" style="display:none">
      <div class="sec-lbl">SOURCE IMAGE</div>
      <div class="edit-dz" id="editDz">
        <input type="file" id="editFile" accept="image/*" onchange="pickEditFile(this.files[0])">
        <div id="editDzTxt">📁 Upload image to edit</div>
        <img id="editPrev" class="edit-prev">
      </div>
      <div class="sec-lbl" style="margin-top:10px">EDIT STRENGTH</div>
      <div class="edit-chips">
        <button class="ec" data-str="0.35" onclick="setEC(this)">Subtle</button>
        <button class="ec active" data-str="0.65" onclick="setEC(this)">Moderate</button>
        <button class="ec" data-str="0.85" onclick="setEC(this)">Strong</button>
      </div>
    </div>

    <div>
      <div class="sec-lbl">PROMPT</div>
      <textarea class="ig-prompt" id="prompt" placeholder="a futuristic city skyline at sunset with flying cars and holographic billboards, cinematic lighting, 8k detail…"></textarea>
    </div>

    <div>
      <div class="sec-lbl">STYLE</div>
      <div class="presets" id="presets">
        <button class="preset" data-append=", photorealistic, DSLR, 4k sharp" onclick="addPreset(this)">Photorealistic</button>
        <button class="preset" data-append=", cinematic film still, anamorphic, color graded" onclick="addPreset(this)">Cinematic</button>
        <button class="preset" data-append=", anime illustration, cel shaded, Studio Ghibli quality" onclick="addPreset(this)">Anime</button>
        <button class="preset" data-append=", oil painting, impasto, museum quality" onclick="addPreset(this)">Oil Painting</button>
        <button class="preset" data-append=", digital concept art, artstation quality" onclick="addPreset(this)">Digital Art</button>
        <button class="preset" data-append=", watercolor painting, soft edges, paper texture" onclick="addPreset(this)">Watercolor</button>
        <button class="preset" data-append=", 3D render, octane render, hyperrealistic" onclick="addPreset(this)">3D Render</button>
        <button class="preset" data-append=", pencil sketch, fine line art, detailed" onclick="addPreset(this)">Sketch</button>
      </div>
    </div>

    <div>
      <div class="sec-lbl">RESOLUTION</div>
      <div class="res-grid">
        <button class="res-btn active" data-w="1024" data-h="1024" onclick="setRes(this)">
          <div class="res-icon"><div style="width:22px;height:22px"></div></div>
          <span class="res-lbl">Square</span>
        </button>
        <button class="res-btn" data-w="768" data-h="1280" onclick="setRes(this)">
          <div class="res-icon"><div style="width:16px;height:24px"></div></div>
          <span class="res-lbl">Portrait</span>
        </button>
        <button class="res-btn" data-w="1280" data-h="768" onclick="setRes(this)">
          <div class="res-icon"><div style="width:26px;height:16px"></div></div>
          <span class="res-lbl">Landscape</span>
        </button>
        <button class="res-btn" data-w="1536" data-h="640" onclick="setRes(this)">
          <div class="res-icon"><div style="width:30px;height:12px"></div></div>
          <span class="res-lbl">Wide</span>
        </button>
      </div>
      <div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:6px" id="resDim">1024 × 1024 px</div>
    </div>

    <div>
      <div class="sec-lbl">STEPS &nbsp;<span id="stepsVal" style="color:var(--purple2)">9</span></div>
      <div class="slider-wrap">
        <input type="range" id="steps" min="1" max="16" value="9" style="--pct:50%"
          oninput="document.getElementById('stepsVal').textContent=this.value;this.style.setProperty('--pct',((this.value-1)/15*100)+'%')">
      </div>
    </div>

    <div>
      <div class="sec-lbl">SEED</div>
      <div class="seed-row">
        <input type="text" class="seed-inp" id="seed" placeholder="random">
        <button class="seed-btn" onclick="randSeed()" title="randomize">⚄</button>
      </div>
    </div>

    <div>
      <button class="adv-toggle" onclick="toggleAdv()">
        <span class="adv-arrow" id="advArrow">›</span> Advanced (negative prompt)
      </button>
      <div class="adv-panel" id="advPanel">
        <div class="sec-lbl">NEGATIVE PROMPT</div>
        <textarea class="neg-inp" id="negPrompt" placeholder="blur, watermark, text, low quality, distorted…"></textarea>
      </div>
    </div>

    <button class="gen-btn" id="genBtn" onclick="igAction()">
      <div class="btn-glow"></div>
      <span class="btn-txt">✦ Generate</span>
    </button>
    <div class="progress-bar" id="progBar"><div class="pb-fill"></div></div>
    <div class="ig-status" id="status"></div>
  </aside>

  <!-- RIGHT: Result + Gallery -->
  <main class="ig-right">
    <div>
      <div class="result-area" id="resultArea">
        <div class="result-placeholder" id="placeholder">
          <div class="big">✦</div>
          <p>Your image will appear here. Describe what you want to create in the prompt panel.</p>
        </div>
        <img class="result-img" id="resultImg" style="display:none">
        <div class="result-overlay">
          <button class="res-act" id="dlBtn" onclick="dlResult()">⬇ Download</button>
          <button class="res-act" onclick="copyPrompt()">📋 Copy prompt</button>
          <span class="res-stats" id="resStats"></span>
        </div>
      </div>
    </div>

    <div>
      <div class="gallery-header">
        <span class="gallery-ttl">RECENT GENERATIONS</span>
        <span class="gallery-count" id="galCount">0</span>
      </div>
      <div class="gallery-grid" id="gallery"></div>
    </div>
  </main>
</div>

<!-- Lightbox -->
<div class="lb" id="lb" onclick="closeLb(event)">
  <div class="lb-inner">
    <button class="lb-close" onclick="closeLb()">✕</button>
    <img class="lb-img" id="lbImg">
    <div class="lb-actions">
      <a class="lb-btn" id="lbDl" download>⬇ Download</a>
      <button class="lb-btn" id="lbCopy" onclick="copyLbPrompt()">📋 Copy prompt</button>
    </div>
  </div>
</div>

<script>
const PROMPTS_KEY='law_img_prompts';
let curFile='',curPrompt='',curW=1024,curH=1024,selModel='qwen',igMode='gen',editFile=null,editStr=0.65;
function setMode(m){
  igMode=m;
  document.getElementById('tab-gen').classList.toggle('active',m==='gen');
  document.getElementById('tab-edit').classList.toggle('active',m==='edit');
  document.getElementById('gen-section').style.display=m==='gen'?'':'none';
  document.getElementById('edit-section').style.display=m==='edit'?'':'none';
  document.getElementById('genBtn').querySelector('.btn-txt').textContent=m==='gen'?'✦ Generate':'✎ Apply Edit';
}
function pickEditFile(f){
  if(!f)return;editFile=f;
  document.getElementById('editDzTxt').textContent=f.name.slice(0,28);
  const prev=document.getElementById('editPrev');prev.src=URL.createObjectURL(f);prev.style.display='block';
}
function setEC(btn){
  editStr=parseFloat(btn.dataset.str)||0.65;
  document.querySelectorAll('.ec').forEach(b=>b.classList.toggle('active',b===btn));
}
async function igAction(){if(igMode==='edit')await applyEdit();else await gen();}
async function applyEdit(){
  const prompt=document.getElementById('prompt').value.trim();
  const st=document.getElementById('status');
  if(!prompt){st.textContent='Enter an edit prompt';st.className='ig-status error';return;}
  if(!editFile){st.textContent='Upload a source image first';st.className='ig-status error';return;}
  const btn=document.getElementById('genBtn');
  btn.disabled=true;btn.querySelector('.btn-txt').textContent='Editing…';
  document.getElementById('progBar').classList.add('active');
  st.textContent='Applying edit… (FLUX.1-dev must be downloaded on first run — ~17 GB)';st.className='ig-status';
  document.getElementById('placeholder').style.display='flex';
  document.getElementById('resultImg').style.display='none';
  const t0=Date.now();
  const fd=new FormData();fd.append('image',editFile);fd.append('prompt',prompt);fd.append('strength',String(editStr));
  try{
    const r=await j('/api/imagine-edit',{method:'POST',body:fd});
    const d=await r.json();const elapsed=((Date.now()-t0)/1000).toFixed(1);
    if(r.ok){
      const u='/api/image-file/'+d.file+'?t='+Date.now();
      curFile=d.file;curPrompt=prompt;savePrompt(d.file,prompt);
      document.getElementById('placeholder').style.display='none';
      const img=document.getElementById('resultImg');img.src=u;img.style.display='block';
      document.getElementById('resStats').textContent='edit · str='+editStr+' · '+elapsed+'s';
      st.textContent='Edit done in '+elapsed+'s';st.className='ig-status ok';
      loadGallery();
    }else{
      const pre=document.createElement('pre');
      pre.style.cssText='white-space:pre-wrap;font-size:0.78rem;margin:0;color:inherit';
      pre.textContent=d.error||'failed';st.textContent='';st.appendChild(pre);
      st.className='ig-status error';
    }
  }catch(e){st.textContent='⚠ '+e;st.className='ig-status error';}
  btn.disabled=false;btn.querySelector('.btn-txt').textContent='✎ Apply Edit';
  document.getElementById('progBar').classList.remove('active');
}

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function j(u,o){const r=await fetch(u,o);if(r.status==401){location='/login';throw 0;}return r;}

// model
const LIC={
  'qwen':         'Qwen/Qwen-Image 20.4B · 4-bit · Apache-2.0 · best quality (downloads ~10 GB on first run)',
  'ernie':        'ERNIE-Image 8B · 4-bit · Apache-2.0 · high quality, fast (downloads ~4 GB on first run)',
  'z-image-turbo':'Z-Image Turbo · cached · Tongyi Qianwen · fastest (ready now)',
};
const DEF_STEPS={'qwen':20,'ernie':20,'z-image-turbo':9};
function setModel(btn){
  selModel=btn.dataset.model;
  document.querySelectorAll('.mc').forEach(b=>b.classList.toggle('active',b===btn));
  document.getElementById('licNote').textContent=LIC[selModel]||'';
  const s=DEF_STEPS[selModel]||9;
  const sl=document.getElementById('steps');sl.value=s;
  document.getElementById('stepsVal').textContent=s;
  sl.style.setProperty('--pct',((s-1)/15*100)+'%');
}

// resolution
function setRes(btn){
  curW=+btn.dataset.w;curH=+btn.dataset.h;
  document.querySelectorAll('.res-btn').forEach(b=>b.classList.toggle('active',b===btn));
  document.getElementById('resDim').textContent=curW+' × '+curH+' px';
}

// style presets
function addPreset(btn){
  const ta=document.getElementById('prompt');
  const app=btn.dataset.append||'';
  if(!ta.value.includes(app.trim()))ta.value+=(ta.value.endsWith(' ')||!ta.value?'':' ')+app.trim();
}

// advanced
function toggleAdv(){
  const p=document.getElementById('advPanel'),a=document.getElementById('advArrow');
  const open=p.classList.toggle('open');a.classList.toggle('open',open);
  a.textContent=open?'›':'›';
  a.style.transform=open?'rotate(90deg)':'';
}

// seed
function randSeed(){document.getElementById('seed').value=Math.floor(Math.random()*999999);}

// prompt storage
function savePrompt(file,prompt){
  try{const p=JSON.parse(localStorage.getItem(PROMPTS_KEY)||'{}');p[file]=prompt;localStorage.setItem(PROMPTS_KEY,JSON.stringify(p));}catch(e){}
}
function getPrompt(file){try{return (JSON.parse(localStorage.getItem(PROMPTS_KEY)||'{}'))[file]||'';}catch(e){return '';}}

// generate
async function gen(){
  const prompt=document.getElementById('prompt').value.trim();
  const st=document.getElementById('status');
  if(!prompt){st.textContent='Enter a prompt first';st.className='ig-status error';return;}
  const steps=+document.getElementById('steps').value||9;
  const seed=(document.getElementById('seed').value||'').trim();
  const neg=document.getElementById('negPrompt').value.trim();
  const fullPrompt=neg?prompt+'. '+neg:prompt;
  const body={prompt:fullPrompt,model:selModel,steps,width:curW,height:curH};
  if(seed&&/^\d+$/.test(seed))body.seed=seed;
  const btn=document.getElementById('genBtn');
  btn.disabled=true;btn.querySelector('.btn-txt').textContent='Generating…';
  document.getElementById('progBar').classList.add('active');
  st.textContent='Generating… (first run may take a few minutes while the model loads)';st.className='ig-status';
  document.getElementById('placeholder').style.display='flex';
  document.getElementById('resultImg').style.display='none';
  const t0=Date.now();
  try{
    const r=await j('/api/imagine',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    const elapsed=((Date.now()-t0)/1000).toFixed(1);
    if(r.ok){
      const u='/api/image-file/'+d.file+'?t='+Date.now();
      curFile=d.file;curPrompt=prompt;
      savePrompt(d.file,prompt);
      document.getElementById('placeholder').style.display='none';
      const img=document.getElementById('resultImg');img.src=u;img.style.display='block';
      document.getElementById('dlBtn').onclick=()=>{const a=document.createElement('a');a.href=u;a.download=d.file;a.click();};
      document.getElementById('resStats').textContent=curW+'×'+curH+' · '+steps+'steps · '+elapsed+'s';
      st.textContent='Done in '+elapsed+'s';st.className='ig-status ok';
      loadGallery();
    }else{
      const msg=d.error||'failed';
      if(r.status===422){
        // model not downloaded — show a friendly multi-line message
        const pre=document.createElement('pre');
        pre.style.cssText='white-space:pre-wrap;font-size:0.78rem;margin:0;color:inherit';
        pre.textContent=msg;
        st.textContent='';st.appendChild(pre);
      }else{st.textContent='⚠ '+msg;}
      st.className='ig-status error';
    }
  }catch(e){st.textContent='⚠ '+e;st.className='ig-status error';}
  btn.disabled=false;btn.querySelector('.btn-txt').textContent='✦ Generate';
  document.getElementById('progBar').classList.remove('active');
}

function dlResult(){
  if(!curFile)return;
  const a=document.createElement('a');a.href='/api/image-file/'+curFile+'?t='+Date.now();a.download=curFile;a.click();
}
function copyPrompt(){
  if(!curPrompt)return;
  navigator.clipboard.writeText(curPrompt).catch(()=>{});
  const st=document.getElementById('status');
  const prev=st.textContent;st.textContent='Prompt copied!';
  setTimeout(()=>st.textContent=prev,2000);
}

// gallery
let lbPrompt='';
function openLb(src,file){
  lbPrompt=getPrompt(file)||'';
  document.getElementById('lbImg').src=src;
  const dl=document.getElementById('lbDl');dl.href=src;dl.download=file;
  document.getElementById('lb').classList.add('open');
}
function closeLb(e){if(!e||e.target===document.getElementById('lb'))document.getElementById('lb').classList.remove('open');}
function copyLbPrompt(){
  if(lbPrompt)navigator.clipboard.writeText(lbPrompt).catch(()=>{});
  const b=document.getElementById('lbCopy');const p=b.textContent;b.textContent='Copied!';setTimeout(()=>b.textContent=p,1500);
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('lb').classList.remove('open');});

async function loadGallery(){
  try{
    const d=await(await j('/api/images')).json();
    const g=document.getElementById('gallery');g.innerHTML='';
    const imgs=d.images||[];
    document.getElementById('galCount').textContent=imgs.length;
    imgs.forEach(f=>{
      const u='/api/image-file/'+f;
      const prompt=getPrompt(f)||'';
      const el=document.createElement('div');el.className='gal-item';
      el.innerHTML='<img src="'+u+'" loading="lazy"><div class="gal-overlay">'+
        (prompt?'<div class="gal-prompt">'+esc(prompt)+'</div>':'')+
        '</div><a class="gal-dl" href="'+u+'" download="'+f+'" onclick="event.stopPropagation()">⬇</a>'+
        '<button class="gal-del" title="Delete from disk" onclick="delGalleryFile(event,\''+f+'\',\'image\')">🗑</button>';
      el.onclick=()=>openLb(u,f);
      g.appendChild(el);
    });
  }catch(e){}
}
async function delGalleryFile(ev,fname,kind){
  ev.stopPropagation();
  if(!confirm('Delete '+fname+' permanently from this machine?'))return;
  const r=await fetch('/api/delete-file',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:fname,kind:kind})});
  if(r.ok){loadGallery();}
  else{try{const d=await r.json();alert('Delete failed: '+(d.error||'unknown'));}catch(e){alert('Delete failed');}}
}
loadGallery();
</script>
</body></html>"""

@app.route("/models")
@require_auth
def models_page():
    return Response(MODELS_PAGE.replace('<!--NAV_CSS-->', NAV_CSS).replace('<!--NAV-->', _build_nav('models')), mimetype="text/html")

# ── vision: prefer the resident mlx_vlm.server (warm, fast); fall back to a fresh
#    bounded subprocess (always works) if that server isn't up ─────────────────
import tempfile as _tmp, base64 as _b64
VISION_MODEL = ENV.get("MLX_VISION_MODEL", "mlx-community/Qwen3-VL-8B-Instruct-4bit")
P_VISION = os.environ.get("MLX_VISION_PORT", "8081")
_VCODE = ("import sys\nfrom mlx_vlm import load, generate\n"
          "from mlx_vlm.prompt_utils import apply_chat_template\nfrom mlx_vlm.utils import load_config\n"
          "repo,img,q=sys.argv[1],sys.argv[2],sys.argv[3]\n"
          "model,proc=load(repo);cfg=load_config(repo)\n"
          "fp=apply_chat_template(proc,cfg,q,num_images=1)\n"
          "out=generate(model,proc,fp,[img],verbose=False,max_tokens=512)\n"
          "print((getattr(out,'text',None) or str(out)).strip())\n")

def _vision_http(model, path, prompt, timeout=300):
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(ext, "png")
    with open(path, "rb") as fh:
        url = f"data:image/{mime};base64," + _b64.b64encode(fh.read()).decode()
    body = {"model": model, "max_tokens": 512, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}}]}]}
    r = requests.post(f"http://127.0.0.1:{P_VISION}/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()

@app.route("/api/vision", methods=["POST"])
@require_auth
def api_vision():
    prompt = (request.form.get("prompt") or "Describe this image in detail.").strip()
    model = (request.form.get("model") or VISION_MODEL).strip()
    f = request.files.get("image")
    if not f: return jsonify({"error": "no image uploaded"}), 400
    suffix = os.path.splitext(f.filename or "img.png")[1] or ".png"
    path = _tmp.mktemp(suffix=suffix); f.save(path)
    try:
        try:                                   # fast path: warm resident vision server
            ans = _vision_http(model, path, prompt)
            if ans: return jsonify({"answer": ans})
        except Exception:
            pass                               # fall back to the always-works subprocess
        try:
            r = subprocess.run([sys.executable, "-c", _VCODE, model, path, prompt],
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "vision timed out (600s) — model too large or multimodal path hanging"}), 504
        if r.returncode != 0:
            return jsonify({"error": (r.stderr or "vision failed").strip()[-600:]}), 500
        return jsonify({"answer": (r.stdout or "").strip()})
    finally:
        try: os.remove(path)
        except Exception: pass

@app.route("/vision")
@require_auth
def vision_page():
    return Response(VISION_PAGE.replace('<!--NAV_CSS-->', NAV_CSS).replace('<!--NAV-->', _build_nav('vision')), mimetype="text/html")

# ── chat (streams tokens from the LiteLLM gateway — OWUI-style, in the dashboard) ──
@app.route("/api/chat", methods=["POST"])
@require_auth
def api_chat():
    d = request.get_json(force=True, silent=True) or {}
    model = d.get("model"); messages = d.get("messages")
    if not model or not isinstance(messages, list) or not messages:
        return jsonify({"error": "model and non-empty messages required"}), 422
    def gen():
        try:
            with requests.post(f"http://127.0.0.1:{P_GATEWAY}/v1/chat/completions",
                               json={"model": model, "messages": messages,
                                     "max_tokens": 2048, "stream": True},
                               stream=True, timeout=600) as r:
                if r.status_code >= 400:
                    yield f"[error: gateway returned {r.status_code}]"; return
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0].get("delta", {}).get("content", "")
                    except Exception:
                        continue
                    if delta:
                        yield delta
        except Exception as e:
            yield f"\n[stream error: {str(e)[:200]}]"
    return Response(gen(), mimetype="text/plain; charset=utf-8")

@app.route("/chat")
@require_auth
def chat_page():
    return Response(CHAT_PAGE.replace('<!--NAV_CSS-->', NAV_CSS).replace('<!--NAV-->', _build_nav('chat')), mimetype="text/html")

# ── agentic chat: runs the query through the AGENT (web_search, web_fetch,
#    self-upgrade, subagents) and streams its live steps + final answer ───────
_AGENT_LOCK = _th.Lock()
_AGENT_BUSY = {"on": False}

@app.route("/api/agent-chat", methods=["POST"])
@require_auth
def api_agent_chat():
    if agent is None:
        return jsonify({"error": "agent module not loaded"}), 500
    d = request.get_json(force=True, silent=True) or {}
    message = (d.get("message") or "").strip()
    persona_name = (d.get("persona") or "orchestrator").strip()
    hist = d.get("history") if isinstance(d.get("history"), list) else []
    history = [{"role": h.get("role"), "content": h.get("content")}
               for h in hist if isinstance(h, dict) and h.get("role") in ("user", "assistant")][-20:]
    if not message:
        return jsonify({"error": "message required"}), 400
    personas = agent.load_personas()
    persona = personas.get(persona_name) or personas.get("orchestrator") or next(iter(personas.values()), None)
    if not persona:
        return jsonify({"error": "no personas configured"}), 500
    with _AGENT_LOCK:
        if _AGENT_BUSY["on"]:
            return jsonify({"error": "agent is busy with another request — one at a time"}), 429
        _AGENT_BUSY["on"] = True

    q = _queue.Queue()
    autonomous = not AUTH_ON     # on localhost act independently; when exposed, refuse gated actions
    def ask_fn(prompt, can_all):
        q.put(("trace", ("✔ auto-approved: " if autonomous else "⛔ needs approval (do it via CLI/Telegram): ") + str(prompt)[:140]))
        return "y" if autonomous else "n"
    def choice_fn(question, options):
        opts = [str(o) for o in (options or [])]
        q.put(("trace", "• " + str(question)[:120]))
        return opts[0] if opts else ""
    def emit_fn(line):
        q.put(("trace", str(line)))

    def worker():
        try:
            agent.EMIT_FN = emit_fn; agent.ASK_FN = ask_fn; agent.CHOICE_FN = choice_fn
            ans = agent.run_agent(message, persona, history=history)
        except Exception as e:
            ans = f"[error: {e}]"
        finally:
            agent.EMIT_FN = agent.ASK_FN = agent.CHOICE_FN = None
            q.put(("answer", ans)); q.put(None)
            _AGENT_BUSY["on"] = False
    _th.Thread(target=worker, daemon=True).start()

    def gen():
        strip = getattr(agent, "strip_thinking", lambda s: s)
        while True:
            item = q.get()
            if item is None:
                break
            typ, text = item
            if typ == "answer":
                text = strip(text) or "[no answer]"
            yield json.dumps({"type": typ, "text": text}) + "\n"
    return Response(gen(), mimetype="application/x-ndjson")

# ── image generation (mflux / FLUX, via the tested wrapper) ─────────────────
from flask import send_file
IMAGES_DIR = Path(os.environ.get("MLX_IMAGES_DIR", str(Path.home() / "MLX-AI" / "documents" / "images")))
IMG_WRAPPER = str(WORKDIR / "mlx-image.sh")

_VALID_MODELS = {"qwen", "ernie", "z-image-turbo", "schnell", "dev"}
_MODEL_STEPS  = {"qwen": 20, "ernie": 20, "z-image-turbo": 9, "schnell": 4, "dev": 20}

@app.route("/api/imagine", methods=["POST"])
@require_auth
def api_imagine():
    d = request.get_json(force=True, silent=True) or {}
    prompt = (d.get("prompt") or "").strip()
    if not prompt: return jsonify({"error": "prompt required"}), 400
    model = d.get("model") if d.get("model") in _VALID_MODELS else "qwen"
    def _int(v, lo, hi, dflt):
        try: return max(lo, min(hi, int(v)))
        except Exception: return dflt
    steps = _int(d.get("steps"), 1, 50, _MODEL_STEPS.get(model, 20))
    w = _int(d.get("width"), 256, 1536, 1024); h = _int(d.get("height"), 256, 1536, 1024)
    seed = str(d.get("seed") or "").strip()
    if seed and not seed.isdigit(): seed = ""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    out = str(IMAGES_DIR / f"img_{int(_time.time())}.png")
    if not os.path.exists(IMG_WRAPPER):
        return jsonify({"error": "image tool missing — run --bootstrap"}), 500
    args = ["bash", IMG_WRAPPER, prompt, out, model, str(steps), str(w), str(h)] + ([seed] if seed else [])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "image generation timed out — first run downloads the model (~10 GB for Qwen). Try again in a few minutes."}), 504
    if r.returncode != 0 or not os.path.exists(out):
        err = (r.stderr or r.stdout or "generation failed").strip()[-800:]
        return jsonify({"error": err}), 500
    return jsonify({"file": os.path.basename(out)})

@app.route("/api/image-file/<name>")
@require_auth
def api_image_file(name):
    if not re.match(r"^[A-Za-z0-9._-]+\.png$", name): return ("bad name", 400)
    p = IMAGES_DIR / name
    if not p.exists(): return ("not found", 404)
    return send_file(str(p), mimetype="image/png")

@app.route("/api/images")
@require_auth
def api_images():
    if not IMAGES_DIR.exists(): return jsonify({"images": []})
    files = sorted((f.name for f in IMAGES_DIR.glob("*.png")), reverse=True)[:24]
    return jsonify({"images": files})

@app.route("/api/delete-file", methods=["POST"])
@require_auth
def api_delete_file():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    kind = data.get("kind", "image")
    if kind == "video":
        if not re.match(r"^[A-Za-z0-9._-]+\.mp4$", name): return jsonify({"error": "bad name"}), 400
        p = VIDEOS_DIR / name
    elif kind == "audio":
        if not re.match(r"^[A-Za-z0-9._-]+\.(wav|mp3)$", name): return jsonify({"error": "bad name"}), 400
        p = AUDIO_DIR / name
    else:
        if not re.match(r"^[A-Za-z0-9._-]+\.png$", name): return jsonify({"error": "bad name"}), 400
        p = IMAGES_DIR / name
    if not p.exists(): return jsonify({"error": "not found"}), 404
    p.unlink()
    return jsonify({"ok": True})

@app.route("/imagine")
@require_auth
def imagine_page():
    return Response(IMAGINE_PAGE.replace('<!--NAV_CSS-->', NAV_CSS).replace('<!--NAV-->', _build_nav('create')), mimetype="text/html")

@app.route("/api/imagine-edit", methods=["POST"])
@require_auth
def api_imagine_edit():
    prompt = (request.form.get("prompt") or "").strip()
    if not prompt: return jsonify({"error": "prompt required"}), 400
    try: strength = max(0.1, min(0.99, float(request.form.get("strength", 0.65))))
    except Exception: strength = 0.65
    f = request.files.get("image")
    if not f: return jsonify({"error": "source image required"}), 400
    suffix = os.path.splitext(f.filename or "img.png")[1] or ".png"
    src = _tmp.mktemp(suffix=suffix); f.save(src)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    out = str(IMAGES_DIR / f"edit_{int(_time.time())}.png")
    try:
        args = ["mflux-generate-qwen", "--quantize", "4",
                "--image-path", src, "--image-strength", str(strength),
                "--steps", "20", "--width", "1024", "--height", "1024",
                "--prompt", prompt, "--output", out]
        r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(out):
            err = (r.stderr or r.stdout or "edit failed").strip()[-600:]
            return jsonify({"error": err[-400:]}), 500
        return jsonify({"file": os.path.basename(out)})
    finally:
        try: os.remove(src)
        except Exception: pass

# ── video generation ─────────────────────────────────────────────────────────
VIDEOS_DIR = Path(os.environ.get("MLX_VIDEOS_DIR", str(Path.home() / "MLX-AI" / "documents" / "videos")))

VIDEO_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Video · LAW Creative Studio</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<!--NAV_CSS-->
<style>
:root{--bg:#020817;--surf:#0a1628;--card:rgba(13,30,53,.9);--bdr:rgba(14,165,233,.13);--bdrbrt:rgba(14,165,233,.35);
--blue:#0ea5e9;--purple:#7c3aed;--purple2:#a78bfa;--green:#10b981;--red:#ef4444;--amber:#f59e0b;
--text:#e2e8f0;--muted:#94a3b8;--mono:'JetBrains Mono',ui-monospace,monospace;--sans:'Space Grotesk',system-ui,sans-serif;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
  background-image:radial-gradient(ellipse 60% 50% at 20% -10%,rgba(124,58,237,.12),transparent),
    radial-gradient(ellipse 50% 40% at 80% 0%,rgba(14,165,233,.06),transparent);}
a{color:var(--blue);text-decoration:none}
.vw{display:grid;grid-template-columns:380px 1fr;gap:0;min-height:calc(100vh - 56px)}
@media(max-width:900px){.vw{grid-template-columns:1fr}}
.vc{padding:20px;display:flex;flex-direction:column;gap:14px;
  border-right:1px solid var(--bdr);background:rgba(10,22,40,.6);
  position:sticky;top:56px;max-height:calc(100vh - 56px);overflow-y:auto;
  scrollbar-width:thin;scrollbar-color:rgba(124,58,237,.2) transparent}
@media(max-width:900px){.vc{position:static;max-height:none;border-right:none;border-bottom:1px solid var(--bdr)}}
.sec-lbl{font-family:var(--mono);font-size:10px;letter-spacing:.12em;color:var(--muted);margin-bottom:6px}
.mode-tabs{display:flex;border-radius:8px;overflow:hidden;border:1px solid var(--bdr)}
.mode-tab{flex:1;padding:10px;border:none;cursor:pointer;font:600 12px var(--sans);
  background:transparent;color:var(--muted);transition:.15s}
.mode-tab.active{background:rgba(124,58,237,.2);color:var(--purple2)}
.mode-tab:hover:not(.active){background:rgba(255,255,255,.05);color:var(--text)}
.mc{padding:5px 12px;border-radius:20px;border:1px solid var(--bdr);font-size:12px;font-weight:500;
  color:var(--muted);cursor:pointer;transition:.15s;background:transparent;font-family:var(--sans)}
.mc:hover{border-color:var(--purple);color:var(--purple2)}
.mc.active{border-color:var(--purple);background:rgba(124,58,237,.15);color:var(--purple2)}
.vq{width:100%;min-height:100px;padding:12px;border-radius:10px;
  border:1px solid var(--bdr);background:rgba(13,30,53,.8);color:var(--text);
  font:14px var(--sans);resize:vertical;outline:none;transition:.2s;line-height:1.5}
.vq:focus{border-color:var(--purple);box-shadow:0 0 0 2px rgba(124,58,237,.12)}
.drop-zone{border:2px dashed rgba(124,58,237,.2);border-radius:12px;padding:24px;
  text-align:center;cursor:pointer;transition:.2s;position:relative;font-size:12px;color:var(--muted)}
.drop-zone:hover,.drop-zone.drag{border-color:var(--purple);background:rgba(124,58,237,.04)}
.drop-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop-prev{max-width:100%;max-height:140px;border-radius:8px;display:none;margin-top:8px;border:1px solid var(--bdr)}
.preset-chips{display:flex;gap:5px;flex-wrap:wrap}
.preset{padding:4px 9px;border-radius:12px;font-size:11px;cursor:pointer;border:1px solid var(--bdr);
  color:var(--muted);transition:.15s;background:transparent;font-family:var(--sans)}
.preset:hover{border-color:var(--purple);color:var(--purple2)}
.slider-wrap{display:flex;align-items:center;gap:10px}
.slider-wrap input[type=range]{flex:1;-webkit-appearance:none;height:4px;border-radius:2px;
  background:linear-gradient(90deg,var(--purple) var(--pct,50%),rgba(255,255,255,.1) var(--pct,50%));outline:none;cursor:pointer}
.slider-wrap input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--purple);box-shadow:0 0 6px rgba(124,58,237,.4)}
.slider-val{font-family:var(--mono);font-size:13px;color:var(--purple2);min-width:36px;text-align:right}
.res-chips{display:flex;gap:6px}
.rc{padding:5px 12px;border-radius:8px;border:1px solid var(--bdr);font-size:12px;font-family:var(--mono);
  color:var(--muted);cursor:pointer;transition:.15s;background:transparent}
.rc.active{border-color:var(--blue);background:rgba(14,165,233,.12);color:var(--blue)}
.gen-btn{width:100%;padding:14px;border:none;border-radius:10px;cursor:pointer;
  font:600 15px var(--sans);color:#fff;transition:.2s;position:relative;overflow:hidden;
  background:linear-gradient(135deg,var(--purple),#6d28d9)}
.gen-btn:not(:disabled):hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(124,58,237,.4)}
.gen-btn:disabled{opacity:.6;cursor:default;transform:none}
.prog{height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden;display:none;margin-top:-4px}
.prog.on{display:block}
.pb{height:100%;background:linear-gradient(90deg,var(--purple),var(--blue),var(--purple));background-size:200% 100%;animation:pb 1.5s linear infinite}
@keyframes pb{0%{background-position:0 0}100%{background-position:200% 0}}
.ig-status{font-size:11px;color:var(--muted);min-height:14px;font-family:var(--mono)}
.ig-status.error{color:var(--red)}.ig-status.ok{color:var(--green)}
.vr{padding:20px;display:flex;flex-direction:column;gap:20px;min-width:0}
.result-area{border-radius:14px;border:1px solid var(--bdr);background:var(--card);
  overflow:hidden;min-height:200px;display:flex;align-items:center;justify-content:center;position:relative}
.result-ph{display:flex;flex-direction:column;align-items:center;gap:14px;padding:80px 20px;
  color:var(--muted);text-align:center}
.result-ph .ico{font-size:56px;opacity:.2}
.result-ph p{font-size:13px;max-width:300px;line-height:1.6}
video{width:100%;border-radius:14px;display:none;max-height:560px;background:#000}
.vid-acts{position:absolute;bottom:0;left:0;right:0;padding:10px 14px;
  background:linear-gradient(transparent,rgba(2,8,23,.9));display:flex;gap:8px;opacity:0;transition:.2s}
.result-area:hover .vid-acts{opacity:1}
.va{padding:6px 12px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid rgba(255,255,255,.15);background:rgba(2,8,23,.7);color:var(--text);
  backdrop-filter:blur(8px);font-family:var(--sans)}
.va:hover{background:rgba(255,255,255,.1)}
.gal-section{display:flex;flex-direction:column;gap:10px}
.gal-hdr{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--muted)}
.gal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}
.gal-item{border-radius:8px;overflow:hidden;cursor:pointer;border:1px solid var(--bdr);
  aspect-ratio:16/9;background:var(--surf);transition:.2s;position:relative}
.gal-item:hover{border-color:var(--bdrbrt);transform:scale(1.02)}
.gal-item video{width:100%;height:100%;object-fit:cover}
.gal-dl{position:absolute;bottom:4px;right:4px;background:rgba(2,8,23,.75);
  border-radius:5px;font-size:11px;padding:3px 7px;color:var(--text);text-decoration:none;
  opacity:0;transition:.2s}
.gal-del{position:absolute;bottom:4px;left:4px;background:rgba(239,68,68,.75);border:none;
  border-radius:5px;font-size:11px;padding:3px 7px;color:#fff;cursor:pointer;opacity:0;transition:.2s}
.gal-item:hover .gal-dl,.gal-item:hover .gal-del{opacity:1}
.install-note{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.2);border-radius:10px;
  padding:14px;font-size:12px;line-height:1.7;color:var(--amber);font-family:var(--mono)}
.install-note code{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:4px;font-size:11px}
</style></head><body>
<!--NAV-->
<div class="vw">
  <aside class="vc">
    <div>
      <div style="font-family:var(--mono);font-size:11px;color:var(--purple2);letter-spacing:.08em;margin-bottom:2px">🎬 CREATIVE STUDIO</div>
      <div class="mode-tabs" style="margin-top:8px">
        <button class="mode-tab active" id="tab-t2v" onclick="setMode('t2v')">📝 Text → Video</button>
        <button class="mode-tab" id="tab-i2v" onclick="setMode('i2v')">🖼 Image → Video</button>
      </div>
    </div>
    <div>
      <div class="sec-lbl">MODEL</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="mc active" data-model="ltx" data-note="LTX-2.3 · T2V+I2V+Audio · fast · ~21GB" onclick="setModel(this)">LTX-2.3 ⚡</button>
        <button class="mc" data-model="wan" data-note="Wan2.2 5B · T2V+I2V · best quality · Apache 2.0 · ~10GB" onclick="setModel(this)">Wan2.2 ★</button>
      </div>
      <div class="ig-status" id="modelNote" style="margin-top:6px">LTX-2.3 · T2V+I2V+Audio · fast · ~21GB on first run</div>
    </div>
    <div id="i2v-section" style="display:none">
      <div class="sec-lbl">SOURCE IMAGE</div>
      <div class="drop-zone" id="imgDz" ondragover="doDrag(event,true)" ondragleave="doDrag(event,false)" ondrop="doDrop(event)">
        <input type="file" id="imgFile" accept="image/*" onchange="pickImg(this.files[0])">
        <div>🖼 Drag &amp; drop or click to upload starting frame</div>
        <img id="imgPrev" class="drop-prev">
      </div>
    </div>
    <div>
      <div class="sec-lbl">PROMPT</div>
      <textarea class="vq" id="prompt" placeholder="A cinematic drone shot over a misty mountain valley at sunrise, golden hour light, 4K quality, smooth camera movement…"></textarea>
      <div class="preset-chips" style="margin-top:8px">
        <button class="preset" onclick="ap('cinematic drone shot, smooth camera movement, golden hour lighting')">Cinematic</button>
        <button class="preset" onclick="ap('timelapse of clouds moving over mountains, dramatic sky')">Timelapse</button>
        <button class="preset" onclick="ap('slow motion, bokeh, shallow depth of field')">Slow-mo</button>
        <button class="preset" onclick="ap('anime style, vibrant colors, dynamic motion')">Anime</button>
        <button class="preset" onclick="ap('photorealistic, DSLR quality, natural lighting')">Realistic</button>
        <button class="preset" onclick="ap('abstract art, colorful fluid motion, hypnotic')">Abstract</button>
      </div>
    </div>
    <div>
      <div class="sec-lbl">TEXT ENCODER</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap" id="encChips">
        <button class="mc active" data-enc="mlx-community/gemma-3-12b-it-4bit" onclick="setEnc(this)">Gemma 3 12B</button>
        <button class="mc" data-enc="mlx-community/gemma-4-12B-it-4bit" onclick="setEnc(this)">Gemma 4 12B</button>
      </div>
      <div class="ig-status" id="encNote" style="margin-top:4px;font-size:11px">Gemma 3 12B 4-bit · ~8 GB · default (downloading)</div>
    </div>
    <div>
      <div class="sec-lbl">DURATION &nbsp;<span id="durVal" style="color:var(--purple2)">5s</span></div>
      <div class="slider-wrap">
        <input type="range" id="dur" min="3" max="15" value="5" style="--pct:15%"
          oninput="document.getElementById('durVal').textContent=this.value+'s';this.style.setProperty('--pct',((this.value-3)/12*100)+'%')">
      </div>
    </div>
    <div>
      <div class="sec-lbl">ASPECT</div>
      <div class="res-chips">
        <button class="rc active" data-w="16" data-h="9" onclick="setAR(this)">16:9</button>
        <button class="rc" data-w="1" data-h="1" onclick="setAR(this)">1:1</button>
        <button class="rc" data-w="9" data-h="16" onclick="setAR(this)">9:16</button>
        <button class="rc" data-w="21" data-h="9" onclick="setAR(this)">21:9</button>
      </div>
    </div>
    <button class="gen-btn" id="genBtn" onclick="generate()">
      <span id="btnTxt">🎬 Generate Video</span>
    </button>
    <div class="prog" id="prog"><div class="pb"></div></div>
    <div class="ig-status" id="status"></div>
  </aside>
  <main class="vr">
    <div class="result-area" id="resultArea">
      <div class="result-ph" id="resPh">
        <div class="ico">🎬</div>
        <p>Your video will appear here. Describe a scene in the prompt panel and click Generate.</p>
        <div style="font-size:11px;font-family:var(--mono);color:rgba(148,163,184,.5);margin-top:8px">LTX-2.3 · ~45s per 5s clip · Audio included</div>
      </div>
      <video id="resultVid" controls></video>
      <div class="vid-acts" id="vidActs">
        <button class="va" onclick="dlVideo()">⬇ Download</button>
        <button class="va" onclick="clearResult()">✕ Clear</button>
        <span style="margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--muted)" id="vidStats"></span>
      </div>
    </div>
    <div class="gal-section">
      <div class="gal-hdr">RECENT GENERATIONS <span id="galCnt" style="background:rgba(255,255,255,.06);padding:2px 7px;border-radius:10px;font-size:10px">0</span></div>
      <div class="gal-grid" id="gallery"></div>
    </div>
    <div class="install-note">
      <strong>First-time setup:</strong> Models download automatically on first generation.<br>
      <strong>LTX-2.3</strong> (~21 GB): <code>pip install mlx-video-with-audio</code> — fast, includes audio<br>
      <strong>Wan2.2 5B</strong> (~10 GB): <code>pip install git+https://github.com/Blaizzy/mlx-video.git</code> — Apache 2.0, best quality<br>
      Generation time: ~45s for LTX-2.3 · ~15–25 min for Wan2.2 (per 5-second clip on M5 Pro)
    </div>
  </main>
</div>
<script>
let vMode='t2v',selModel='ltx',imgFile=null,curVideoFile='',arW=16,arH=9;
let selEnc='mlx-community/gemma-3-12b-it-4bit';
const MODEL_NOTES={'ltx':'LTX-2.3 · T2V+I2V+Audio · fast · ~21GB on first run','wan':'Wan2.2 5B · best motion quality · Apache 2.0 · ~10GB on first run'};
const ENC_NOTES={'mlx-community/gemma-3-12b-it-4bit':'Gemma 3 12B 4-bit · ~8 GB · default (downloading)','mlx-community/gemma-4-12B-it-4bit':'Gemma 4 12B 4-bit · ~6.7 GB · multimodal · downloads on first use'};
function setMode(m){
  vMode=m;
  document.getElementById('tab-t2v').classList.toggle('active',m==='t2v');
  document.getElementById('tab-i2v').classList.toggle('active',m==='i2v');
  document.getElementById('i2v-section').style.display=m==='i2v'?'':'none';
}
function setModel(btn){
  selModel=btn.dataset.model;
  document.querySelectorAll('#chips .mc,button[data-model]').forEach(b=>b.classList.toggle('active',b===btn));
  document.getElementById('modelNote').textContent=MODEL_NOTES[selModel]||'';
}
function setEnc(btn){
  selEnc=btn.dataset.enc;
  document.querySelectorAll('#encChips .mc').forEach(b=>b.classList.toggle('active',b===btn));
  document.getElementById('encNote').textContent=ENC_NOTES[selEnc]||selEnc;
}
function setAR(btn){
  arW=+btn.dataset.w;arH=+btn.dataset.h;
  document.querySelectorAll('.rc').forEach(b=>b.classList.toggle('active',b===btn));
}
function ap(t){const ta=document.getElementById('prompt');if(!ta.value.includes(t.trim()))ta.value=(ta.value+' '+t).trim();}
function doDrag(e,on){e.preventDefault();document.getElementById('imgDz').classList.toggle('drag',on);}
function doDrop(e){
  e.preventDefault();document.getElementById('imgDz').classList.remove('drag');
  const f=[...(e.dataTransfer.files||[])].find(x=>x.type.startsWith('image/'));if(f)pickImg(f);
}
function pickImg(f){
  if(!f)return;imgFile=f;
  const p=document.getElementById('imgPrev');p.src=URL.createObjectURL(f);p.style.display='block';
  document.querySelector('#imgDz div').textContent='📸 '+f.name.slice(0,30);
}
let _vidPollTimer=null;
async function generate(){
  const prompt=document.getElementById('prompt').value.trim();
  const st=document.getElementById('status');
  if(!prompt){st.textContent='Enter a prompt first';st.className='ig-status error';return;}
  if(vMode==='i2v'&&!imgFile){st.textContent='Upload a source image for Image→Video';st.className='ig-status error';return;}
  const btn=document.getElementById('genBtn'),dur=document.getElementById('dur').value;
  btn.disabled=true;document.getElementById('prog').classList.add('on');
  document.getElementById('resPh').style.display='flex';
  document.getElementById('resultVid').style.display='none';
  const t0=Date.now();
  const fd=new FormData();
  fd.append('prompt',prompt);fd.append('model',selModel);fd.append('duration',dur);
  fd.append('aspect',arW+':'+arH);fd.append('mode',vMode);fd.append('encoder',selEnc);
  if(imgFile)fd.append('image',imgFile);
  try{
    const r=await fetch('/api/video',{method:'POST',body:fd});
    if(r.status===401){location='/login';return;}
    const d=await r.json();
    if(!r.ok){
      const pre=document.createElement('pre');
      pre.style.cssText='white-space:pre-wrap;font-size:0.78rem;margin:0;color:inherit;word-break:break-word';
      pre.textContent=d.error||'failed';st.textContent='';st.appendChild(pre);
      st.className='ig-status error';
      btn.disabled=false;document.getElementById('prog').classList.remove('on');
      return;
    }
    // Start polling for job completion
    const jobId=d.job_id;
    st.textContent='⏳ Started — first run downloads LTX-2.3 (~21GB), then ~45s generation…';st.className='ig-status';
    if(_vidPollTimer)clearInterval(_vidPollTimer);
    _vidPollTimer=setInterval(async()=>{
      try{
        const jr=await fetch('/api/video-job/'+jobId);
        const jd=await jr.json();
        const elapsed=jd.elapsed||0;
        const mins=Math.floor(elapsed/60),secs=elapsed%60;
        const timeStr=mins?mins+'m '+secs+'s':secs+'s';
        if(jd.status==='running'){
          st.textContent='⏳ Generating… '+timeStr+' elapsed'+(elapsed<120?' (downloading model on first run)':'');
          st.className='ig-status';
        }else if(jd.status==='done'){
          clearInterval(_vidPollTimer);_vidPollTimer=null;
          const vid=document.getElementById('resultVid');
          vid.src='/api/video-file/'+jd.file+'?t='+Date.now();
          vid.style.display='block';
          document.getElementById('resPh').style.display='none';
          document.getElementById('vidStats').textContent=dur+'s · '+timeStr+' total';
          curVideoFile=jd.file;
          st.textContent='✓ Done in '+timeStr;st.className='ig-status ok';
          btn.disabled=false;document.getElementById('prog').classList.remove('on');
          loadGallery();
        }else{
          clearInterval(_vidPollTimer);_vidPollTimer=null;
          const pre=document.createElement('pre');
          pre.style.cssText='white-space:pre-wrap;font-size:0.78rem;margin:0;color:inherit;word-break:break-word';
          pre.textContent=jd.error||'failed';st.textContent='';st.appendChild(pre);
          st.className='ig-status error';
          btn.disabled=false;document.getElementById('prog').classList.remove('on');
        }
      }catch(e){}
    },5000);
  }catch(e){st.textContent='⚠ '+e;st.className='ig-status error';btn.disabled=false;document.getElementById('prog').classList.remove('on');}
}
function dlVideo(){
  if(!curVideoFile)return;
  const a=document.createElement('a');a.href='/api/video-file/'+curVideoFile;a.download=curVideoFile;a.click();
}
function clearResult(){
  document.getElementById('resultVid').style.display='none';
  document.getElementById('resPh').style.display='flex';
}
async function loadGallery(){
  try{
    const d=await(await fetch('/api/videos')).json();
    const g=document.getElementById('gallery');g.innerHTML='';
    const vids=d.videos||[];document.getElementById('galCnt').textContent=vids.length;
    vids.forEach(f=>{
      const u='/api/video-file/'+f;
      const el=document.createElement('div');el.className='gal-item';
      el.innerHTML='<video src="'+u+'" muted loop></video>'+
        '<a class="gal-dl" href="'+u+'" download="'+f+'" onclick="event.stopPropagation()">⬇</a>'+
        '<button class="gal-del" title="Delete from disk" onclick="delGalleryFile(event,\''+f+'\',\'video\')">🗑</button>';
      el.querySelector('video').onmouseover=function(){this.play();};
      el.querySelector('video').onmouseout=function(){this.pause();};
      el.onclick=()=>{document.getElementById('resultVid').src=u+'&t='+Date.now();document.getElementById('resultVid').style.display='block';document.getElementById('resPh').style.display='none';curVideoFile=f;};
      g.appendChild(el);
    });
  }catch(e){}
}
async function delGalleryFile(ev,fname,kind){
  ev.stopPropagation();
  if(!confirm('Delete '+fname+' permanently from this machine?'))return;
  const r=await fetch('/api/delete-file',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:fname,kind:kind})});
  if(r.ok){loadGallery();}
  else{try{const d=await r.json();alert('Delete failed: '+(d.error||'unknown'));}catch(e){alert('Delete failed');}}
}
loadGallery();
</script></body></html>"""

import uuid as _uuid
_VIDEO_JOBS: dict = {}  # job_id → {status, file, error, start, prompt}

def _run_video_job(job_id, cmd, out, src_path):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if r.returncode != 0 or not os.path.exists(out):
            err = (r.stderr or r.stdout or "generation failed").strip()[-800:]
            _VIDEO_JOBS[job_id].update({"status": "error", "error": err[-400:]})
        else:
            _VIDEO_JOBS[job_id].update({"status": "done", "file": os.path.basename(out)})
    except subprocess.TimeoutExpired:
        _VIDEO_JOBS[job_id].update({"status": "error", "error": "Timed out after 2 hours"})
    except Exception as e:
        _VIDEO_JOBS[job_id].update({"status": "error", "error": str(e)})
    finally:
        if src_path:
            try: os.remove(src_path)
            except Exception: pass

@app.route("/api/video", methods=["POST"])
@require_auth
def api_video():
    prompt = (request.form.get("prompt") or "").strip()
    if not prompt: return jsonify({"error": "prompt required"}), 400
    model = request.form.get("model", "ltx").strip()
    if model == "wan":
        return jsonify({"error": "Wan2.2 requires: pip install git+https://github.com/Blaizzy/mlx-video.git — then restart dashboard. Use LTX-2.3 (already installed)."}), 501
    try: duration = max(3, min(30, int(request.form.get("duration", 5))))
    except Exception: duration = 5
    mode = request.form.get("mode", "t2v")
    _ALLOWED_ENCODERS = {
        "mlx-community/gemma-3-12b-it-4bit",
        "mlx-community/gemma-4-12B-it-4bit",
    }
    encoder = request.form.get("encoder", "mlx-community/gemma-3-12b-it-4bit").strip()
    if encoder not in _ALLOWED_ENCODERS:
        encoder = "mlx-community/gemma-3-12b-it-4bit"
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    out = str(VIDEOS_DIR / f"vid_{int(_time.time())}.mp4")
    src_path = None
    f = request.files.get("image")
    if f and mode == "i2v":
        suffix = os.path.splitext(f.filename or "img.png")[1] or ".png"
        src_path = _tmp.mktemp(suffix=suffix); f.save(src_path)
    cmd = [sys.executable, "-m", "mlx_video.generate_av",
           "--prompt", prompt, "--output-path", out,
           "--model-repo", "notapalindrome/ltx23-mlx-av-q4",
           "--text-encoder-repo", encoder,
           "--num-frames", str(min(257, duration * 24))]
    if src_path:
        cmd += ["--image", src_path, "--image-strength", "0.85"]
    job_id = str(_uuid.uuid4())[:8]
    _VIDEO_JOBS[job_id] = {"status": "running", "file": None, "error": None,
                           "start": _time.time(), "prompt": prompt[:80]}
    _th.Thread(target=_run_video_job, args=(job_id, cmd, out, src_path), daemon=True).start()
    return jsonify({"job_id": job_id, "status": "running"})

@app.route("/api/video-job/<job_id>")
@require_auth
def api_video_job(job_id):
    job = _VIDEO_JOBS.get(job_id)
    if not job: return jsonify({"status": "not_found"}), 404
    elapsed = int(_time.time() - job["start"])
    return jsonify({**job, "elapsed": elapsed})

@app.route("/api/video-file/<name>")
@require_auth
def api_video_file(name):
    if not re.match(r"^[A-Za-z0-9._-]+\.mp4$", name): return ("bad name", 400)
    p = VIDEOS_DIR / name
    if not p.exists(): return ("not found", 404)
    return send_file(str(p), mimetype="video/mp4")

@app.route("/api/videos")
@require_auth
def api_videos():
    if not VIDEOS_DIR.exists(): return jsonify({"videos": []})
    files = sorted((f.name for f in VIDEOS_DIR.glob("*.mp4")), reverse=True)[:20]
    return jsonify({"videos": files})

@app.route("/video")
@require_auth
def video_page():
    return Response(VIDEO_PAGE.replace('<!--NAV_CSS-->', NAV_CSS).replace('<!--NAV-->', _build_nav('video')), mimetype="text/html")

# ── audio studio ─────────────────────────────────────────────────────────────
AUDIO_DIR = Path(os.environ.get("MLX_AUDIO_DIR", str(Path.home() / "MLX-AI" / "documents" / "audio")))

AUDIO_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audio Studio · LAW</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<!--NAV_CSS-->
<style>
:root{--bg:#020817;--surf:#0a1628;--card:rgba(13,30,53,.9);--bdr:rgba(14,165,233,.13);
--blue:#0ea5e9;--purple:#7c3aed;--purple2:#a78bfa;--green:#10b981;--red:#ef4444;--amber:#f59e0b;
--text:#e2e8f0;--muted:#94a3b8;--mono:'JetBrains Mono',ui-monospace,monospace;--sans:'Space Grotesk',system-ui,sans-serif;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
  background-image:radial-gradient(ellipse 60% 50% at 50% -20%,rgba(16,185,129,.08),transparent),
    radial-gradient(ellipse 40% 30% at 90% 0%,rgba(124,58,237,.06),transparent);}
a{color:var(--blue);text-decoration:none}
.aw{max-width:900px;margin:0 auto;padding:24px 20px}
.page-hdr{margin-bottom:20px}
.page-title{font-size:22px;font-weight:700;display:flex;align-items:center;gap:10px}
.page-sub{color:var(--muted);font-size:13px;margin-top:4px}
.tab-bar{display:flex;gap:2px;border-bottom:1px solid var(--bdr);margin-bottom:20px}
.tab{padding:10px 18px;font-size:13px;font-weight:600;cursor:pointer;border:none;background:transparent;
  color:var(--muted);border-bottom:2px solid transparent;transition:.15s;margin-bottom:-1px;font-family:var(--sans)}
.tab:hover{color:var(--text)}
.tab.active{color:var(--green);border-bottom-color:var(--green)}
.tab-pane{display:none}.tab-pane.active{display:flex;flex-direction:column;gap:16px}
.card{background:var(--card);border:1px solid var(--bdr);border-radius:14px;padding:20px;backdrop-filter:blur(12px)}
.sec-lbl{font-family:var(--mono);font-size:10px;letter-spacing:.12em;color:var(--muted);margin-bottom:8px}
.vq{width:100%;min-height:90px;padding:12px;border-radius:10px;border:1px solid var(--bdr);
  background:rgba(13,30,53,.8);color:var(--text);font:14px var(--sans);resize:vertical;outline:none;transition:.2s;line-height:1.5}
.vq:focus{border-color:var(--green);box-shadow:0 0 0 2px rgba(16,185,129,.12)}
.voice-chips{display:flex;gap:6px;flex-wrap:wrap}
.vc{padding:5px 12px;border-radius:20px;border:1px solid var(--bdr);font-size:12px;
  color:var(--muted);cursor:pointer;transition:.15s;background:transparent;font-family:var(--sans)}
.vc:hover{border-color:var(--green);color:var(--green)}
.vc.active{border-color:var(--green);background:rgba(16,185,129,.15);color:var(--green)}
.slider-wrap{display:flex;align-items:center;gap:10px}
.slider-wrap input[type=range]{flex:1;-webkit-appearance:none;height:4px;border-radius:2px;
  background:linear-gradient(90deg,var(--green) var(--pct,50%),rgba(255,255,255,.1) var(--pct,50%));outline:none;cursor:pointer}
.slider-wrap input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--green);box-shadow:0 0 6px rgba(16,185,129,.4)}
.slider-val{font-family:var(--mono);font-size:13px;color:var(--green);min-width:36px;text-align:right}
.gen-btn{width:100%;padding:14px;border:none;border-radius:10px;cursor:pointer;
  font:600 15px var(--sans);color:#fff;transition:.2s;
  background:linear-gradient(135deg,var(--green),#059669)}
.gen-btn:not(:disabled):hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(16,185,129,.35)}
.gen-btn:disabled{opacity:.6;cursor:default;transform:none}
.prog{height:3px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden;display:none;margin-top:-4px}
.prog.on{display:block}
.pb{height:100%;background:linear-gradient(90deg,var(--green),var(--blue),var(--green));background-size:200% 100%;animation:pb 1.5s linear infinite}
@keyframes pb{0%{background-position:0 0}100%{background-position:200% 0}}
.st{font-size:11px;font-family:var(--mono);color:var(--muted);min-height:14px}
.st.ok{color:var(--green)}.st.err{color:var(--red)}
.audio-player{width:100%;border-radius:10px;margin-top:4px;accent-color:var(--green)}
.gal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px;margin-top:8px}
.gal-item{background:rgba(10,22,40,.8);border:1px solid var(--bdr);border-radius:10px;padding:12px;
  display:flex;flex-direction:column;gap:6px}
.gal-name{font-family:var(--mono);font-size:11px;color:var(--muted);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gal-audio{width:100%;accent-color:var(--green)}
.gal-dl{font-size:11px;color:var(--blue);display:flex;align-items:center;justify-content:space-between}
.gal-del-btn{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.25);border-radius:5px;
  font-size:10px;padding:2px 8px;color:#ef4444;cursor:pointer;transition:.15s}
.gal-del-btn:hover{background:rgba(239,68,68,.25)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:600px){.two-col{grid-template-columns:1fr}}
.music-chat{display:flex;flex-direction:column;gap:12px;min-height:80px;max-height:420px;overflow-y:auto;padding:4px 0}
.music-msg{display:flex;flex-direction:column;gap:6px;background:rgba(10,22,40,.7);border:1px solid var(--bdr);border-radius:10px;padding:14px}
.music-msg-hdr{font-family:var(--mono);font-size:10px;color:var(--muted);display:flex;align-items:center;gap:8px}
.music-msg audio{width:100%;accent-color:var(--purple2);margin-top:4px}
.music-msg-dl{font-size:11px;color:var(--blue)}
</style></head><body>
<!--NAV-->
<div class="aw">
  <div class="page-hdr">
    <div class="page-title">🎵 Audio Studio</div>
    <div class="page-sub">Text-to-speech, voice cloning, and AI music generation — 100% local.</div>
  </div>
  <div class="tab-bar">
    <button class="tab active" onclick="switchTab('tts',this)">🗣 Text-to-Speech</button>
    <button class="tab" onclick="switchTab('music',this)">🎸 Music Studio</button>
    <button class="tab" onclick="switchTab('gallery',this)">📁 Library</button>
  </div>

  <!-- TTS TAB -->
  <div class="tab-pane active" id="tab-tts">
    <div class="card">
      <div class="sec-lbl">TEXT</div>
      <textarea class="vq" id="ttsText" placeholder="Enter text to convert to speech…&#10;&#10;Tip: Use punctuation for natural pauses. Add [excited] or [whispering] tone hints." rows="5"></textarea>
    </div>
    <div class="card">
      <div class="sec-lbl">VOICE</div>
      <div class="voice-chips" id="voiceChips">
        <button class="vc active" data-voice="af_sky" onclick="setVoice(this)">af_sky — Female · Warm</button>
        <button class="vc" data-voice="af_heart" onclick="setVoice(this)">af_heart — Female · Soft</button>
        <button class="vc" data-voice="am_adam" onclick="setVoice(this)">am_adam — Male · Deep</button>
        <button class="vc" data-voice="bm_lewis" onclick="setVoice(this)">bm_lewis — British · Male</button>
        <button class="vc" data-voice="bf_emma" onclick="setVoice(this)">bf_emma — British · Female</button>
        <button class="vc" data-voice="am_echo" onclick="setVoice(this)">am_echo — Male · Resonant</button>
      </div>
    </div>
    <div class="card">
      <div class="two-col">
        <div>
          <div class="sec-lbl">SPEED &nbsp;<span id="speedVal" style="color:var(--green)">1.0×</span></div>
          <div class="slider-wrap">
            <input type="range" id="speed" min="5" max="20" value="10" style="--pct:33%"
              oninput="document.getElementById('speedVal').textContent=(this.value/10).toFixed(1)+'×';this.style.setProperty('--pct',((this.value-5)/15*100)+'%')">
          </div>
        </div>
        <div>
          <div class="sec-lbl">FORMAT</div>
          <div style="display:flex;gap:6px">
            <button class="vc active" data-fmt="wav" onclick="setFmt(this)">WAV</button>
            <button class="vc" data-fmt="mp3" onclick="setFmt(this)">MP3</button>
          </div>
        </div>
      </div>
    </div>
    <button class="gen-btn" id="ttsBtn" onclick="genTTS()">🗣 Generate Speech</button>
    <div class="prog" id="ttsProg"><div class="pb"></div></div>
    <div class="st" id="ttsStatus"></div>
    <audio class="audio-player" id="ttsPlayer" controls style="display:none"></audio>
  </div>

  <!-- MUSIC TAB -->
  <div class="tab-pane" id="tab-music">
    <div class="card" id="musicHistCard" style="display:none">
      <div class="sec-lbl">GENERATED TRACKS</div>
      <div class="music-chat" id="musicHistory"></div>
    </div>
    <div class="card">
      <div class="sec-lbl">PROMPT — describe the music in plain English</div>
      <textarea class="vq" id="musicPrompt" rows="3"
        placeholder="e.g. create a hiphop song explaining quantum computing with energetic beats and rap lyrics&#10;e.g. relaxing jazz piano for late night coding sessions&#10;e.g. epic orchestral track for a sci-fi movie trailer"></textarea>
    </div>
    <div id="aceStatus" style="font-size:11px;font-family:var(--mono);color:var(--muted)">Checking ACE-Step…</div>
    <button class="gen-btn" id="musicBtn" onclick="generateMusic()">🎵 Generate Music</button>
    <div class="prog" id="musicProg"><div class="pb"></div></div>
    <div class="st" id="musicStatus"></div>
  </div>

  <!-- GALLERY TAB -->
  <div class="tab-pane" id="tab-gallery">
    <div class="card">
      <div class="sec-lbl">AUDIO LIBRARY</div>
      <div class="gal-grid" id="audGallery"><div style="color:var(--muted);font-size:13px">Loading…</div></div>
    </div>
  </div>
</div>
<script>
let selVoice='af_sky',selFmt='wav';
function switchTab(name,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if(name==='gallery')loadGallery();
  if(name==='music')checkAceStep();
}
function setVoice(btn){selVoice=btn.dataset.voice;document.querySelectorAll('.vc').forEach(b=>b.classList.toggle('active',b===btn));}
function setFmt(btn){selFmt=btn.dataset.fmt;document.querySelectorAll('[data-fmt]').forEach(b=>b.classList.toggle('active',b===btn));}
async function genTTS(){
  const text=document.getElementById('ttsText').value.trim();
  const st=document.getElementById('ttsStatus');
  if(!text){st.textContent='Enter text first';st.className='st err';return;}
  const btn=document.getElementById('ttsBtn');
  btn.disabled=true;document.getElementById('ttsProg').classList.add('on');
  st.textContent='Generating speech… (first run downloads Kokoro ~327 MB)';st.className='st';
  const speed=document.getElementById('speed').value/10;
  const t0=Date.now();
  try{
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,voice:selVoice,speed,format:selFmt})});
    if(r.status===401){location='/login';return;}
    const d=await r.json();
    const elapsed=((Date.now()-t0)/1000).toFixed(1);
    if(r.ok){
      const pl=document.getElementById('ttsPlayer');
      pl.src='/api/audio-file/'+d.file+'?t='+Date.now();pl.style.display='block';
      pl.play().catch(()=>{});
      st.textContent='Done in '+elapsed+'s';st.className='st ok';
    }else{
      const pre=document.createElement('pre');
      pre.style.cssText='white-space:pre-wrap;font-size:0.78rem;margin:0';
      pre.textContent=d.error||'failed';st.textContent='';st.appendChild(pre);
      st.className='st err';
    }
  }catch(e){st.textContent='⚠ '+e;st.className='st err';}
  btn.disabled=false;document.getElementById('ttsProg').classList.remove('on');
}
let aceRunning=false,musicJobId=null,musicPollT=null;
async function checkAceStep(){
  const el=document.getElementById('aceStatus');
  try{
    const r=await fetch('/api/music-status',{signal:AbortSignal.timeout(4000)});
    const d=await r.json();
    aceRunning=d.running;
    if(d.running){
      el.innerHTML='<span style="color:var(--green)">● ACE-Step ready</span> · <a href="http://localhost:7860" target="_blank" style="font-size:10px;color:var(--blue)">Open UI →</a>';
    }else if(d.starting){
      el.innerHTML='<span style="color:var(--blue)">⏳ ACE-Step starting…</span>';
      setTimeout(checkAceStep,6000);
    }else{
      el.innerHTML='<span style="color:var(--amber)">● ACE-Step not running</span> · <button onclick="startAceStep()" style="font-size:10px;padding:2px 8px;border:1px solid var(--amber);background:transparent;color:var(--amber);border-radius:4px;cursor:pointer">▶ Start</button>';
    }
  }catch(e){
    el.innerHTML='<span style="color:var(--muted)">● ACE-Step status unknown</span>';
  }
}
async function startAceStep(){
  document.getElementById('aceStatus').innerHTML='<span style="color:var(--blue)">⏳ Starting ACE-Step…</span>';
  try{
    await fetch('/api/music-start',{method:'POST'});
    setTimeout(checkAceStep,8000);
  }catch(e){}
}
async function generateMusic(){
  const prompt=document.getElementById('musicPrompt').value.trim();
  if(!prompt)return;
  if(!aceRunning){
    document.getElementById('musicStatus').textContent='ACE-Step not running — click Start first';
    document.getElementById('musicStatus').className='st err';
    return;
  }
  const btn=document.getElementById('musicBtn');
  btn.disabled=true;
  document.getElementById('musicProg').classList.add('on');
  document.getElementById('musicStatus').textContent='Agent is planning your music…';
  document.getElementById('musicStatus').className='st';
  try{
    const r=await fetch('/api/music-generate',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({prompt})});
    const d=await r.json();
    if(!r.ok){
      document.getElementById('musicStatus').textContent=d.error||'Generation failed';
      document.getElementById('musicStatus').className='st err';
      btn.disabled=false;document.getElementById('musicProg').classList.remove('on');
      return;
    }
    musicJobId=d.job_id;
    document.getElementById('musicStatus').textContent='Generating music (2–5 min)… '+d.plan_summary;
    pollMusicJob();
  }catch(e){
    document.getElementById('musicStatus').textContent='Error: '+e;
    document.getElementById('musicStatus').className='st err';
    btn.disabled=false;document.getElementById('musicProg').classList.remove('on');
  }
}
function pollMusicJob(){
  if(!musicJobId)return;
  if(musicPollT)clearTimeout(musicPollT);
  musicPollT=setTimeout(async()=>{
    try{
      const r=await fetch('/api/music-job/'+musicJobId);
      const d=await r.json();
      if(d.status==='done'){
        document.getElementById('musicProg').classList.remove('on');
        document.getElementById('musicBtn').disabled=false;
        document.getElementById('musicStatus').textContent='Done in '+d.elapsed+'s';
        document.getElementById('musicStatus').className='st ok';
        addMusicCard(d);
        musicJobId=null;
      }else if(d.status==='error'){
        document.getElementById('musicProg').classList.remove('on');
        document.getElementById('musicBtn').disabled=false;
        document.getElementById('musicStatus').textContent=d.error||'Generation failed';
        document.getElementById('musicStatus').className='st err';
        musicJobId=null;
      }else{
        const elapsed=d.elapsed||0;
        document.getElementById('musicStatus').textContent='Generating… '+elapsed+'s elapsed';
        pollMusicJob();
      }
    }catch(e){pollMusicJob();}
  },4000);
}
function addMusicCard(d){
  const hist=document.getElementById('musicHistory');
  const hc=document.getElementById('musicHistCard');
  hc.style.display='';
  const card=document.createElement('div');card.className='music-msg';
  const u='/api/audio-file/'+d.file;
  card.innerHTML='<div class="music-msg-hdr"><span style="color:var(--purple2)">🎵 '+d.prompt_short+'</span><span style="color:var(--muted);margin-left:auto">'+d.elapsed+'s · '+d.caption+'</span></div>'+
    '<audio controls src="'+u+'"></audio>'+
    '<div class="music-msg-dl"><a href="'+u+'" download="'+d.file+'">⬇ Download</a>'+
    (d.lyrics?'<span style="color:var(--muted);font-size:10px">Lyrics: '+d.lyrics.substring(0,60)+'…</span>':'')+'</div>';
  hist.appendChild(card);
  hist.scrollTop=hist.scrollHeight;
  card.querySelector('audio').play().catch(()=>{});
}
checkAceStep();
async function loadGallery(){
  try{
    const d=await(await fetch('/api/audios')).json();
    const g=document.getElementById('audGallery');g.innerHTML='';
    if(!(d.files||[]).length){g.innerHTML='<div style="color:var(--muted);font-size:13px">No audio generated yet.</div>';return;}
    (d.files||[]).forEach(f=>{
      const u='/api/audio-file/'+f;
      const el=document.createElement('div');el.className='gal-item';
      el.innerHTML='<div class="gal-name">'+f+'</div><audio class="gal-audio" src="'+u+'" controls></audio>'+
        '<div class="gal-dl"><a href="'+u+'" download="'+f+'">⬇ Download</a>'+
        '<button class="gal-del-btn" onclick="delGalleryFile(event,\''+f+'\',\'audio\')">🗑 Delete</button></div>';
      g.appendChild(el);
    });
  }catch(e){}
}
async function delGalleryFile(ev,fname,kind){
  ev.stopPropagation();ev.preventDefault();
  if(!confirm('Delete '+fname+' permanently from this machine?'))return;
  const r=await fetch('/api/delete-file',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:fname,kind:kind})});
  if(r.ok){loadGallery();}
  else{try{const d=await r.json();alert('Delete failed: '+(d.error||'unknown'));}catch(e){alert('Delete failed');}}
}
</script></body></html>"""

@app.route("/api/tts", methods=["POST"])
@require_auth
def api_tts():
    d = request.get_json(force=True, silent=True) or {}
    text = (d.get("text") or "").strip()
    if not text: return jsonify({"error": "text required"}), 400
    voice = (d.get("voice") or "af_sky").strip()
    try: speed = max(0.5, min(2.0, float(d.get("speed", 1.0))))
    except Exception: speed = 1.0
    fmt = "wav" if (d.get("format") or "wav") == "wav" else "mp3"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"tts_{_uuid.uuid4().hex[:12]}"
    # --join_audio merges all sentence chunks into {prefix}.wav (not {prefix}_000.wav)
    out = AUDIO_DIR / f"{prefix}.wav"
    try:
        r = subprocess.run([sys.executable, "-m", "mlx_audio.tts.generate",
                            "--model", "prince-canuma/Kokoro-82M",
                            "--text", text, "--voice", voice, "--speed", str(speed),
                            "--output_path", str(AUDIO_DIR), "--file_prefix", prefix,
                            "--join_audio"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.exists():
            err = (r.stderr or r.stdout or "TTS failed").strip()[-600:]
            return jsonify({"error": err[-400:]}), 500
        return jsonify({"file": out.name})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "TTS timed out (>5 min)"}), 504

_MUSIC_JOBS = {}
_ACESTEP_URL = "http://127.0.0.1:7860"

def _acestep_running():
    try:
        r = requests.get(f"{_ACESTEP_URL}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False

def _run_music_job(job_id, prompt):
    import shutil
    try:
        # /release_task on the Gradio-embedded API is synchronous — blocks until audio is done
        # Use sample_query so ACE-Step's built-in LLM plans the style from plain English
        payload = {
            "sample_query": prompt,   # triggers ACE-Step's own music-planning LLM
            "audio_duration": 60,
            "audio_format": "mp3",
        }
        r = requests.post(f"{_ACESTEP_URL}/release_task", json=payload, timeout=600)
        if r.status_code != 200:
            # LLM not initialized — fall back to raw prompt as caption
            payload2 = {"prompt": prompt, "audio_duration": 60, "audio_format": "mp3"}
            r = requests.post(f"{_ACESTEP_URL}/release_task", json=payload2, timeout=600)
            if r.status_code != 200:
                _MUSIC_JOBS[job_id].update({"status": "error", "error": r.text[:300]})
                return

        resp = r.json().get("data", {})
        task_id = resp.get("task_id")
        if not task_id:
            _MUSIC_JOBS[job_id].update({"status": "error", "error": "No task_id in response"})
            return

        # Generation is already done — fetch stored result immediately
        qr = requests.post(f"{_ACESTEP_URL}/query_result",
                           json={"task_id_list": [task_id]}, timeout=15)
        if qr.status_code != 200:
            _MUSIC_JOBS[job_id].update({"status": "error", "error": "query_result failed"})
            return

        items = qr.json().get("data", [])
        item = items[0] if items else {}
        result_raw = item.get("result", "[]")
        try:
            result_list = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
        except Exception:
            result_list = []

        if not result_list or item.get("status", 0) != 1:
            err = (result_list[0].get("error") if result_list else None) or "No audio returned"
            _MUSIC_JOBS[job_id].update({"status": "error", "error": err})
            return

        audio_path = result_list[0].get("file", "")
        if not audio_path or not Path(audio_path).exists():
            _MUSIC_JOBS[job_id].update({"status": "error", "error": "Audio file not found on disk"})
            return

        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        dest_name = f"music_{_uuid.uuid4().hex[:10]}.mp3"
        shutil.copy2(audio_path, str(AUDIO_DIR / dest_name))
        caption = (result_list[0].get("prompt") or prompt)[:60]
        lyrics = result_list[0].get("lyrics") or ""
        elapsed = int(_time.time() - _MUSIC_JOBS[job_id]["start"])
        _MUSIC_JOBS[job_id].update({
            "status": "done", "file": dest_name, "elapsed": elapsed,
            "caption": caption, "lyrics": lyrics, "prompt_short": prompt[:50],
        })
    except Exception as e:
        _MUSIC_JOBS[job_id].update({"status": "error", "error": str(e)[:300]})

@app.route("/api/music-status")
@require_auth
def api_music_status():
    running = _acestep_running()
    return jsonify({"running": running, "starting": False})

@app.route("/api/music-generate", methods=["POST"])
@require_auth
def api_music_generate():
    d = request.get_json(force=True, silent=True) or {}
    prompt = (d.get("prompt") or "").strip()
    if not prompt: return jsonify({"error": "prompt required"}), 400
    if not _acestep_running():
        return jsonify({"error": "ACE-Step not running. Click Start in Music Studio."}), 503
    job_id = str(_uuid.uuid4())[:8]
    _MUSIC_JOBS[job_id] = {"status": "running", "start": _time.time(), "error": None}
    _th.Thread(target=_run_music_job, args=(job_id, prompt), daemon=True).start()
    return jsonify({"job_id": job_id, "plan_summary": "ACE-Step is composing your track…"})

@app.route("/api/music-job/<job_id>")
@require_auth
def api_music_job(job_id):
    job = _MUSIC_JOBS.get(job_id)
    if not job: return jsonify({"status": "not_found"}), 404
    return jsonify({**job, "elapsed": int(_time.time() - job["start"])})

@app.route("/api/music-start", methods=["POST"])
@require_auth
def api_music_start():
    import subprocess as _sp
    ace_dir = Path.home() / "ace-step"
    if not ace_dir.exists():
        return jsonify({"error": "~/ace-step not found"}), 500
    plist = Path.home() / "Library" / "LaunchAgents" / "com.mlxstack.acestep.plist"
    if plist.exists():
        _sp.run(["launchctl", "start", "com.mlxstack.acestep"], capture_output=True, text=True)
    else:
        _sp.Popen(["/opt/homebrew/bin/uv", "run", "acestep", "--server-name", "0.0.0.0",
                   "--port", "7860", "--enable-api"],
                  cwd=str(ace_dir), stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    return jsonify({"ok": True})

@app.route("/api/audio-file/<name>")
@require_auth
def api_audio_file(name):
    if not re.match(r"^[A-Za-z0-9._-]+\.(wav|mp3)$", name): return ("bad name", 400)
    p = AUDIO_DIR / name
    if not p.exists(): return ("not found", 404)
    mime = "audio/wav" if name.endswith(".wav") else "audio/mpeg"
    return send_file(str(p), mimetype=mime)

@app.route("/api/audios")
@require_auth
def api_audios():
    if not AUDIO_DIR.exists(): return jsonify({"files": []})
    files = sorted((f.name for f in AUDIO_DIR.iterdir() if f.suffix in (".wav", ".mp3")), reverse=True)[:30]
    return jsonify({"files": files})

@app.route("/audio")
@require_auth
def audio_page():
    return Response(AUDIO_PAGE.replace('<!--NAV_CSS-->', NAV_CSS).replace('<!--NAV-->', _build_nav('audio')), mimetype="text/html")

@app.route("/")
@require_auth
def index(): return Response(PAGE, mimetype="text/html")

if __name__ == "__main__":
    app.run(host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"), port=int(os.environ.get("PORT_DASHBOARD", "8800")), debug=False)
