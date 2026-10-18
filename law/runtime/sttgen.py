"""Speech-to-text (voice → words) with mlx-whisper, run as a subprocess.

Telegram voice notes are OGG/Opus; mlx-whisper uses ffmpeg to decode them and whisper-large-v3-turbo
to transcribe (fast, multilingual, high quality — handles English and Malay). Runs out-of-process so
it never fights the language model for the GPU in the single worker (the worker unloads the LLM first,
same as image/tts/video/vision).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

MODEL = "mlx-community/whisper-large-v3-turbo"   # ~1.6 GB, downloads once on first use
DEFAULTS = {"timeout_s": 600}

_SCRIPT = (
    "import json,sys,mlx_whisper\n"
    "r=mlx_whisper.transcribe(sys.argv[1], path_or_hf_repo=sys.argv[2])\n"
    "print('__STT__'+json.dumps({'text':(r.get('text') or '').strip(),'language':r.get('language')}))\n"
)


def venv_python(paths) -> Path:
    return paths.runtime / ".venv" / "bin" / "python"


def available(paths) -> bool:
    py = venv_python(paths)
    if not py.exists():
        return False
    return subprocess.run([str(py), "-c", "import mlx_whisper"], capture_output=True).returncode == 0


def transcribe(paths, audio: str | Path, *, cfg: dict | None = None, model: str | None = None,
               touch=None, check=None, on_log=None) -> dict:
    """Transcribe one audio file. Returns {text, language, seconds}. Raises RuntimeError on failure."""
    c = {**DEFAULTS, **(cfg or {})}
    aud = Path(audio).expanduser()
    if not aud.is_file():
        raise RuntimeError(f"audio not found: {aud}")
    args = [str(venv_python(paths)), "-c", _SCRIPT, str(aud), model or MODEL]
    t0 = time.time()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", start_new_session=True)
    lines: list[str] = []

    def reader():
        for ln in iter(proc.stdout.readline, ""):
            ln = ln.rstrip()
            if ln:
                lines.append(ln)
                if on_log:
                    try:
                        on_log(ln)
                    except Exception:  # noqa: BLE001
                        pass
    import threading
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    deadline = t0 + float(c["timeout_s"])
    try:
        while proc.poll() is None:
            if touch:
                touch()
            if check:
                check()
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError(f"transcription timed out after {c['timeout_s']:.0f}s")
            time.sleep(0.4)
    finally:
        if proc.poll() is None:
            proc.kill()
        th.join(5)
    data = None
    for ln in lines:
        if ln.startswith("__STT__"):
            try:
                data = json.loads(ln[len("__STT__"):])
            except ValueError:
                data = None
    if proc.returncode != 0 or not data:
        tail = "\n".join(lines[-15:])
        raise RuntimeError(f"transcription failed (exit {proc.returncode}). Last output:\n{tail}")
    return {"text": data.get("text", ""), "language": data.get("language"),
            "seconds": round(time.time() - t0, 1)}
