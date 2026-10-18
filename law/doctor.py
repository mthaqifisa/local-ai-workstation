"""`law status`: read-only health report. Never changes anything."""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from . import __version__
from .manifest import load_manifest
from .paths import Paths
from .registry import Registry, load
from .steps.v1 import _present as v1_present
from .steps.v1 import cache_dir
from .util import head, is_macos, ok, out, read_env, warn, which


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _lan_ip() -> str:
    for iface in ("en0", "en1"):
        ip = out(["ipconfig", "getifaddr", iface]) if is_macos() else ""
        if ip:
            return ip
    return ""


def cached(p: Paths, repo: str) -> bool:
    snap = cache_dir(p, repo) / "snapshots"
    return snap.is_dir() and any(snap.iterdir())


def hf_status(repo: str, token: str | None) -> str:
    req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return "gated" if json.loads(r.read()).get("gated") else "ok"
    except urllib.error.HTTPError as e:
        return {401: "gated/private", 403: "gated", 404: "NOT FOUND"}.get(e.code, f"http {e.code}")
    except Exception as e:  # noqa: BLE001
        return f"offline ({type(e).__name__})"


def probe_note(pr: dict, context: int | None) -> str:
    proven = pr.get("proven_context") or pr.get("suggested_context")
    s = f"probe: ~{pr.get('speed', {}).get('tok_s')} tok/s, proven ctx {proven or 'none'}"
    if pr.get("failed_at"):
        s += f", failed at {pr['failed_at']}"
    if context and pr.get("failed_at") and context >= pr["failed_at"]:
        s += " ← context too high"
    return s


def report(p: Paths, *, check_hf: bool = False) -> int:
    print(f"law {__version__}")
    head("Machine")
    if is_macos():
        ram = int(out(["sysctl", "-n", "hw.memsize"]) or 0) // 1024**3
        print(f"  {out(['sysctl', '-n', 'machdep.cpu.brand_string'])} · {ram} GB · "
              f"macOS {out(['sw_vers', '-productVersion'])}")
    else:
        warn("not macOS — limited report")
    from .runtime import memory
    pr = memory.pressure()
    print(f"  memory: {memory.available_gb():.0f} GB free of {memory.total_gb():.0f} GB · pressure {pr}"
          + ("  ← close big apps" if pr == "critical" else ""))
    problems = 0
    try:
        reg: Registry = load(p)
        ok(f"registry valid · {len(reg.models)} models · {len(reg.personas)} personas")
    except Exception as e:  # noqa: BLE001
        warn(f"registry INVALID: {e}")
        return 1

    lan = _lan_ip()
    head(f"Ports  (local · LAN {lan or 'n/a'})")
    local_only = {"inference"}                 # reached only through the queue, on purpose
    for name, port in reg.ports.items():
        loc = _port_open("127.0.0.1", port)
        lan_ok = _port_open(lan, port) if (lan and loc and name not in local_only) else None
        if name in local_only and loc:
            lan_s = "· local only (by design — use the gateway)"
        else:
            lan_s = "" if lan_ok is None else ("· LAN ok" if lan_ok else "· NOT reachable from LAN")
        print(f"  {name:<10} :{port:<5} {'up' if loc else '-':<4} {lan_s}")

    head("Models")
    token = read_env(p.env_file).get("HF_TOKEN")
    man = load_manifest(p)
    from .runtime.probe import load_probes
    probes = load_probes(p)
    for m in reg.models.values():
        flags = ["downloaded" if cached(p, m.repo) else ("MISSING" if m.download == "auto" else "not downloaded")]
        if m.download == "auto" and not cached(p, m.repo):
            problems += 1
        if not reg.fits(m.id)[0]:
            flags.append("TOO BIG")
        if m.source in ("user", "override"):
            flags.append("yours" if m.source == "user" else "your override")
        if check_hf:
            st = hf_status(m.repo, token)
            flags.append(f"hf:{st}")
            problems += st == "NOT FOUND"
        if probes.get(m.id):
            flags.append(probe_note(probes[m.id], m.context))
        print(f"  {m.id:<20} {m.weights_gb:>5g} GB  {m.download:<6} ctx {m.context or '-':<6} {m.repo}  "
              f"[{', '.join(flags)}]")
    stale = [r for r in man.data["models"] if r not in {m.repo for m in reg.models.values()}]
    if stale:
        print("  pending prune (next ./install.sh): " + ", ".join(stale))

    head("Tools (Homebrew)")
    try:
        from .system import System
        installed = System().brew_installed() if is_macos() else set()
    except Exception:  # noqa: BLE001
        installed = set()
    for t in reg.brew_formulae:
        # brew's own list counts: keg-only formulae (e.g. sqlite) are installed but not on PATH
        good = t in installed or bool(which(t))
        problems += not good
        print(f"  {t:<15} {'ok' if good else 'MISSING'}")

    head("Services")
    recent: list = []
    try:
        from .runtime.db import Queue
        if p.queue_db.exists():
            q = Queue(p.queue_db)
            w = q.worker_info()
            if not w or w["state"] == "stopped" or w["age_s"] > 30:
                print("  worker   NOT running → law start   (log: law logs worker)")
                problems += 1
            else:
                busy = f" · job {w['job_id']} last progress {w['progress_age_s']:.0f}s ago" \
                    if w["state"] == "running" else ""
                waiting = sum(1 for j in q.jobs(200) if j["status"] == "waiting")
                print(f"  worker   {w['state']} · model {w['model'] or '-'}{busy} · {q.depth()} queued"
                      + (f" · {waiting} agent(s) waiting for sub-jobs" if waiting else ""))
            recent = q.watchdog_log(3)
        else:
            print("  worker   not started yet (no queue database)")
            problems += 1
    except Exception as e:  # noqa: BLE001
        print(f"  worker   unknown ({e})")
    gw = reg.ports["gateway"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{gw}/health", timeout=3):
            host = lan or "127.0.0.1"
            print(f"  gateway  up · OpenAI http://{host}:{gw}/v1 · Anthropic http://{host}:{gw}")
    except Exception:  # noqa: BLE001
        print(f"  gateway  NOT answering on :{gw} → law restart   (log: law logs gateway)")
        problems += 1
    if is_macos():
        from .system import System
        if System().launchd_loaded("com.law.watchdog"):
            print("  watchdog running")
        else:
            print("  watchdog NOT running → law start   (log: law logs watchdog)")
            problems += 1
    from .telegram import beat_age, configured
    if configured(p):
        age = beat_age(p)
        from .system import System
        loaded = is_macos() and System().launchd_loaded("com.law.telegram")
        if is_macos() and not loaded:
            print("  telegram NOT running → law start   (log: law logs telegram)")
            problems += 1
        else:
            print(f"  telegram running · last poll {'never' if age is None else f'{age:.0f}s ago'}"
                  "   (law telegram status)")
    else:
        print("  telegram not configured (optional) → law configure")
    from . import opencode
    if opencode.in_use(p.home):
        try:
            stale, _ = opencode.needs_update(p.home, reg)
            print("  opencode " + ("provider 'law' needs an update → law setup-opencode" if stale
                                   else "provider 'law' set up (law/coder, law/general, …)"))
        except opencode.JsoncConfig:
            print("  opencode uses opencode.jsonc — add the block from `law setup-opencode --print`")
    for r in recent:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        print(f"           {when} watchdog {r['action']} {r['target']}: {r['reason']}")

    head("v1")
    print("  still present → run ./install.sh to retire it" if v1_present(p) else "  retired / not present")
    return 1 if problems else 0
