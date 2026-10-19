"""ACE-Step music generation (optional). Clones the Apple-Silicon fork to ~/ace-step, installs
its deps with `uv sync`, and registers launchd service com.law.acestep on the acestep port.

ON DEMAND (not at login): ACE-Step keeps its DiT + language model resident — ~15 GB of unified
memory for as long as it runs — which defeats law's "one big model in RAM" rule. So it is
started with `law music start` or the dashboard's Music tile, and the worker stops it
automatically when a model load needs the memory (`law music stop` stops it by hand).

It is a self-contained Gradio app, so law only installs + launches it; the Music-studio tile in
the dashboard (and `law status`) point at its port. Never fails the install — a problem is a
warning and music is simply unavailable until fixed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..engine import Action, Ctx, Step
from ..registry import load

REPO = "https://github.com/clockworksquirrel/ace-step-apple-silicon.git"
LABEL = "com.law.acestep"


def _dir() -> Path:
    return Path.home() / "ace-step"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# ── on-demand control (law music, dashboard, worker) ─────────────────────
def running(port: int = 7860) -> bool:
    import socket
    with socket.socket() as so:
        so.settimeout(0.3)
        return so.connect_ex(("127.0.0.1", port)) == 0


def start() -> str:
    """Start the music studio (loads ~15 GB). Returns a status line."""
    plist = _plist_path()
    if not plist.exists():
        return "ACE-Step is not set up — run: law setup-music"
    uid = os.getuid()
    if subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, timeout=30).returncode:
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, timeout=30)
    subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{LABEL}"], capture_output=True, timeout=30)
    return "starting ACE-Step (it takes ~1 min to load its models)"


def stop() -> bool:
    """Stop the music studio if it runs (frees its ~15 GB). True if it was running."""
    uid = os.getuid()
    r = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, text=True, timeout=30)
    if r.returncode or "pid =" not in r.stdout:
        return False
    subprocess.run(["launchctl", "kill", "SIGTERM", f"gui/{uid}/{LABEL}"], capture_output=True, timeout=30)
    return True


def _plist_body(ctx: Ctx, port: int, host: str = "127.0.0.1") -> str:
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    log = ctx.paths.logs / "acestep.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{uv}</string><string>run</string><string>acestep</string>
    <string>--server-name</string><string>{host}</string>
    <string>--port</string><string>{port}</string><string>--enable-api</string>
  </array>
  <key>WorkingDirectory</key><string>{_dir()}</string>
  <key>RunAtLoad</key><false/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def plan_acestep(ctx: Ctx) -> list[Action]:
    if not (ctx.paths.home / ".law").exists():   # sanity
        return []
    reg = load(ctx.paths)
    port = int(reg.ports.get("acestep", 7860))
    d = _dir()
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    acts: list[Action] = []
    if not (d / ".git").exists():
        acts.append(Action("add", f"clone ACE-Step → {d}",
                           lambda: subprocess.run(["git", "clone", "--depth=1", REPO, str(d)],
                                                  check=True, timeout=600)))
    if not (d / ".venv").exists():
        acts.append(Action("add", "install ACE-Step deps (uv sync)",
                           lambda: subprocess.run([uv, "sync", "--no-dev"], cwd=str(d),
                                                  check=True, timeout=1800)))
    plist, body = _plist_path(), _plist_body(ctx, port, reg.bind_host)   # LAN only if LAW_BIND_HOST says so
    current = plist.read_text() if plist.exists() else ""
    if current != body:
        def install_service():
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(body)
            ctx.sys.launchd_load(LABEL, plist)       # registered, not started (RunAtLoad false)
        acts.append(Action("update" if plist.exists() else "add", f"ACE-Step service ({LABEL}) on :{port}",
                           install_service))
    return acts


STEPS = [Step("acestep", "Music generation (ACE-Step)", plan_acestep, required=False, macos_only=True)]
