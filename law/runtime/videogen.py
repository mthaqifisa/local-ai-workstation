"""Local text/image-to-video generation (LTX-2.3 via mlx-video-with-audio), as a queue job.

Runs as queue kind "video" through the single worker (frees the language model first). LTX-2.3
is the fast option (~45 s per 5 s clip on M5 Pro, audio included). The LTX weights (~21 GB) and
the gemma text-encoder download once on first use. Output: MP4 under ~/MLX-AI/outputs/video/.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

MODEL_REPO = "notapalindrome/ltx23-mlx-av-q4"
ENCODER_REPO = "mlx-community/gemma-3-12b-it-4bit"
FPS = 24
MAX_FRAMES = 257
DEFAULTS = {"duration_s": 5, "timeout_s": 5400}        # first run downloads ~21 GB, so allow a long time


def exe(paths) -> Path | None:
    p = paths.runtime / ".venv" / "bin" / "python"
    return p if p.exists() else None


def available(paths) -> bool:
    import importlib.util  # noqa: F401
    py = exe(paths)
    if py is None:
        return False
    r = subprocess.run([str(py), "-c", "import importlib.util,sys;"
                        "sys.exit(0 if importlib.util.find_spec('mlx_video') else 1)"],
                       capture_output=True, timeout=20)
    return r.returncode == 0


def out_dir(paths) -> Path:
    d = paths.outputs / "video"
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate(paths, prompt: str, *, out: Path | None = None, cfg: dict | None = None,
             image: str | None = None, touch=None, check=None, on_log=None) -> dict:
    """Generate a short video with LTX-2.3. Returns {path, seconds}. Raises RuntimeError on failure."""
    c = {**DEFAULTS, **(cfg or {})}
    py = exe(paths)
    if py is None:
        raise RuntimeError("the model-server venv (with mlx-video) was not found")
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("empty prompt")
    dest = Path(out) if out else (out_dir(paths) / f"vid-{int(time.time())}.mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    frames = max(FPS, min(MAX_FRAMES, int(c["duration_s"]) * FPS))
    args = [str(py), "-m", "mlx_video.generate_av", "--prompt", prompt, "--output-path", str(dest),
            "--model-repo", MODEL_REPO, "--text-encoder-repo", ENCODER_REPO, "--num-frames", str(frames)]
    if image:
        args += ["--image", str(image), "--image-strength", str(c.get("image_strength", 0.85))]
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
                raise RuntimeError(f"video generation timed out after {c['timeout_s']:.0f}s")
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.kill()
        th.join(5)
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(f"video generation failed (exit {proc.returncode}). Last output:\n" + "\n".join(lines[-15:]))
    return {"path": str(dest), "seconds": round(time.time() - t0, 1), "bytes": dest.stat().st_size}
