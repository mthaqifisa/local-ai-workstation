"""macOS notification when a long job ends — so you can walk away from a build or a video render.
Only top-level jobs of the kinds below that ran at least `runtime.notify.after_s` seconds;
chats from Open WebUI / API clients are never notified (you're watching those). Telegram
jobs are skipped too — the bot already tells you. Turn off with runtime.notify.enabled: false."""
from __future__ import annotations

import subprocess
import sys

KINDS = {"agent", "image", "video", "tts", "probe", "index", "vision", "stt"}


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def should_notify(job: dict, status: str, seconds: float, cfg: dict) -> bool:
    return (bool(cfg.get("enabled", True)) and sys.platform == "darwin" and job.get("kind") in KINDS
            and not job.get("parent_id") and job.get("source") not in ("telegram", "api", "anthropic")
            and status in ("done", "failed") and seconds >= float(cfg.get("after_s", 60)))


def send(title: str, text: str, sound: bool = True) -> None:
    script = f'display notification "{_esc(text[:220])}" with title "{_esc(title)}"' + \
        (' sound name "Glass"' if sound else "")
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
