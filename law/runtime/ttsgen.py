"""Local text-to-speech (Kokoro via mlx-audio), as a queue job.

Like image generation, TTS uses the GPU, so it runs as a normal queue job (kind "tts") through
the single worker — never alongside a chat/agent/image job. The worker frees any language model
first, then runs mlx-audio's Kokoro pipeline and saves a WAV under ~/MLX-AI/outputs/audio/.
Fully local, no API keys. Kokoro weights (~327 MB) download once on first use.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

MODEL = "prince-canuma/Kokoro-82M"
DEFAULTS = {"voice": "af_sky", "speed": 1.0, "timeout_s": 600}
# Kokoro voices (a=American, b=British; f=female, m=male).
VOICES = {
    "af_sky": "Female · Warm (US)", "af_heart": "Female · Soft (US)",
    "am_adam": "Male · Deep (US)", "am_echo": "Male · Resonant (US)",
    "bf_emma": "Female · British", "bm_lewis": "Male · British",
}


def exe(paths) -> Path | None:
    p = paths.runtime / ".venv" / "bin" / "python"
    return p if p.exists() else None


def available(paths) -> bool:
    import importlib.util
    py = exe(paths)
    if py is None:
        return False
    # mlx_audio lives in the runtime venv, not the control venv — check via that interpreter.
    r = subprocess.run([str(py), "-c", "import importlib.util,sys;"
                        "sys.exit(0 if importlib.util.find_spec('mlx_audio') else 1)"],
                       capture_output=True, timeout=20)
    return r.returncode == 0


def out_dir(paths) -> Path:
    d = paths.outputs / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate(paths, text: str, *, out: Path | None = None, cfg: dict | None = None,
             touch=None, check=None, on_log=None) -> dict:
    """Synthesize speech with Kokoro. Returns {path, seconds, voice}. Raises RuntimeError on failure."""
    c = {**DEFAULTS, **(cfg or {})}
    py = exe(paths)
    if py is None:
        raise RuntimeError("the model-server venv (with mlx-audio) was not found")
    text = (text or "").strip()
    if not text:
        raise RuntimeError("empty text")
    voice = c["voice"] if c.get("voice") in VOICES else "af_sky"
    dest = Path(out) if out else (out_dir(paths) / f"tts-{int(time.time())}.wav")
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = dest.with_suffix("").name
    args = [str(py), "-m", "mlx_audio.tts.generate", "--model", MODEL, "--text", text,
            "--voice", voice, "--speed", str(c["speed"]), "--output_path", str(dest.parent),
            "--file_prefix", prefix, "--join_audio"]
    # misaki may lazily install its spacy model via uv on first use — point uv at the runtime venv.
    env = {**os.environ, "VIRTUAL_ENV": str(paths.runtime / ".venv"),
           "UV_PROJECT_ENVIRONMENT": str(paths.runtime / ".venv")}
    t0 = time.time()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", start_new_session=True, env=env)
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
                raise RuntimeError(f"text-to-speech timed out after {c['timeout_s']:.0f}s")
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.kill()
        th.join(5)
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(f"text-to-speech failed (exit {proc.returncode}). Last output:\n" + "\n".join(lines[-12:]))
    return {"path": str(dest), "seconds": round(time.time() - t0, 1),
            "voice": voice, "bytes": dest.stat().st_size}
