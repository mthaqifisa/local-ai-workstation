"""Mac app-control helpers used by the Operator role (opening apps, media, volume, screenshots).

Each function wraps a standard macOS command-line tool (open, osascript, shortcuts, screencapture)
and returns plain text so an agent can read the result.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

TIMEOUT_S = 60


def _run(cmd: list[str], timeout: float = TIMEOUT_S) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {out[-500:]}")
    return out


def osascript(script: str) -> str:
    return _run(["osascript", "-e", script])


def open_app(name: str) -> str:
    _run(["open", "-a", name])
    return f"opened {name}"


def quit_app(name: str) -> str:
    osascript(f'tell application "{_q(name)}" to quit')
    return f"quit {name}"


def open_url(url: str) -> str:
    _run(["open", url])
    return f"opened {url}"


def get_volume() -> int:
    return int(osascript("output volume of (get volume settings)"))


def set_volume(percent: int) -> str:
    level = max(0, min(100, int(percent)))
    osascript(f"set volume output volume {level}")
    return f"volume set to {get_volume()}%"


def run_shortcut(name: str, input_text: str | None = None) -> str:
    cmd = ["shortcuts", "run", name]
    if input_text is not None:
        tmp = Path.home() / "LAW" / "cache" / "tmp" / f"shortcut-{int(time.time() * 1000)}.txt"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(input_text)
        cmd += ["--input-path", str(tmp)]
    return _run(cmd, timeout=300) or f"ran shortcut {name}"


def screenshot(dest_dir: Path | None = None) -> str:
    out_dir = dest_dir or Path.home() / "LAW" / "outputs" / "images" / "screens"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    _run(["screencapture", "-x", str(path)])
    return str(path)


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


MEDIA_APPS = ("Spotify", "Music")


def media(app: str, action: str) -> str:
    """play / pause / playpause / next / previous for Spotify or Music."""
    if app not in MEDIA_APPS:
        raise ValueError(f"app must be one of {MEDIA_APPS}")
    verbs = {"play": "play", "pause": "pause", "playpause": "playpause",
             "next": "next track", "previous": "previous track"}
    if action not in verbs:
        raise ValueError(f"action must be one of {sorted(verbs)}")
    osascript(f'tell application "{app}" to {verbs[action]}')
    return now_playing(app)


def play_search(app: str, query: str) -> str:
    """Spotify: open a search and play the top result. Music: play the first library match."""
    if app == "Spotify":
        _run(["open", f"spotify:search:{query}"])
        time.sleep(3)
        osascript('tell application "Spotify" to play')
    elif app == "Music":
        osascript('tell application "Music" to play (first track of library playlist 1 whose name contains "'
                  + _q(query) + '")')
    else:
        raise ValueError(f"app must be one of {MEDIA_APPS}")
    time.sleep(2)
    return now_playing(app)


def now_playing(app: str) -> str:
    state = osascript(f'tell application "{app}" to player state as string')
    if state != "playing":
        return f"{app}: {state}"
    name = osascript(f'tell application "{app}" to name of current track')
    artist = osascript(f'tell application "{app}" to artist of current track')
    return f"{app}: playing {name} by {artist}"
