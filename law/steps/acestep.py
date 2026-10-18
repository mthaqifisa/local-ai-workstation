"""ACE-Step music generation (optional). Clones the Apple-Silicon fork to ~/ace-step, installs
its deps with `uv sync`, and runs it as launchd service com.law.acestep on the acestep port.

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


def _plist_body(ctx: Ctx, port: int) -> str:
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    log = ctx.paths.logs / "acestep.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{uv}</string><string>run</string><string>acestep</string>
    <string>--server-name</string><string>0.0.0.0</string>
    <string>--port</string><string>{port}</string><string>--enable-api</string>
  </array>
  <key>WorkingDirectory</key><string>{_dir()}</string>
  <key>RunAtLoad</key><true/>
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
    plist, body = _plist_path(), _plist_body(ctx, port)
    current = plist.read_text() if plist.exists() else ""
    if current != body:
        def install_service():
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(body)
            uid = os.getuid()
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True, timeout=30)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, timeout=30)
        acts.append(Action("update" if plist.exists() else "add", f"ACE-Step service ({LABEL}) on :{port}",
                           install_service))
    return acts


STEPS = [Step("acestep", "Music generation (ACE-Step)", plan_acestep, required=False, macos_only=True)]
