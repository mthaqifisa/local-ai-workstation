"""Image understanding (vision) with qwen3-vl-8b, run as a subprocess.

mlx-openai-server 1.8.1's VLM batch scheduler is broken with its own pinned mlx-vlm (<0.5): it passes
a `kv_bits` kwarg that mlx-vlm 0.4.4's BatchGenerator doesn't accept, so a multimodal request hangs
forever. So — exactly like image/tts/video generation — we run mlx-vlm's own CLI as a subprocess
(`python -m mlx_vlm generate`), which works reliably and keeps the shared model server untouched.

Used by worker kind "vision": describe/analyse a photo, OCR, "where is this place", etc. The worker
unloads the language model first so the VLM (~7 GB) has the GPU.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

MODEL = "qwen3-vl-8b"
REPO = "mlx-community/Qwen3-VL-8B-Instruct-4bit"
DEFAULT_PROMPT = (
    "Describe this image in detail and transcribe any visible text (signs, shop names, street names). "
    "If it shows a place, street, shop, building or map, identify the most likely location and the "
    "landmarks/text that point to it, then give a Google Maps link in EXACTLY this format on its own "
    "line: https://www.google.com/maps/search/?api=1&query=<the+place+or+address+with+spaces+as+plus+signs>. "
    "If you cannot tell the location, say so instead of guessing a link.")
DEFAULTS = {"max_tokens": 500, "temperature": 0.2, "timeout_s": 600}


def _collapse_loops(text: str) -> str:
    """VLMs sometimes degenerate into repeating the same sentence to fill the token budget. Collapse
    consecutive identical sentences/lines down to one so the answer stays clean."""
    parts = re.split(r"(?<=[.!?\n])\s+", text or "")
    out: list[str] = []
    for p in parts:
        s = p.strip()
        if s and out and s == out[-1].strip():
            continue                     # drop an immediate duplicate sentence
        out.append(p)
    return " ".join(out).strip()


def venv_python(paths) -> Path:
    return paths.runtime / ".venv" / "bin" / "python"


def available(paths) -> bool:
    py = venv_python(paths)
    if not py.exists():
        return False
    r = subprocess.run([str(py), "-c", "import mlx_vlm"], capture_output=True)
    return r.returncode == 0


def _parse(out: str) -> str:
    """Pull the generated answer out of mlx-vlm's CLI output (fenced by '==========', after the
    echoed prompt which ends at the assistant turn marker)."""
    blocks = out.split("==========")
    mid = blocks[1] if len(blocks) >= 2 else out
    for marker in ("<|im_start|>assistant", "assistant\n"):
        if marker in mid:
            mid = mid.split(marker, 1)[1]
            break
    # drop the CLI's own echo lines if they leaked in
    lines = [ln for ln in mid.splitlines() if not ln.startswith(("Files:", "Prompt:", "Generation:",
                                                                 "Peak memory:", "=="))]
    return _collapse_loops("\n".join(lines).strip())


def describe(paths, image: str | Path, prompt: str = "", *, cfg: dict | None = None,
             touch=None, check=None, on_log=None) -> dict:
    """Run qwen3-vl on one image. Returns {text, seconds, model, image}. Raises RuntimeError on failure."""
    c = {**DEFAULTS, **(cfg or {})}
    img = Path(image).expanduser()
    if not img.is_file():
        raise RuntimeError(f"image not found: {img}")
    q = (prompt or DEFAULT_PROMPT).strip()
    # if the user asks about a place/map/link, pin the link FORMAT so the model gives a real, working
    # Google Maps search URL instead of inventing a dead goo.gl short link.
    if re.search(r"\b(map|maps|location|where|address|directions?|place)\b", q, re.I):
        q += (" For any Google Maps link, use EXACTLY this working format and never invent a short "
              "goo.gl link: https://www.google.com/maps/search/?api=1&query=<place+name+with+plus+signs>.")
    args = [str(venv_python(paths)), "-m", "mlx_vlm", "generate", "--model", REPO,
            "--image", str(img), "--prompt", q,
            "--max-tokens", str(int(c["max_tokens"])), "--temperature", str(c["temperature"])]
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
                raise RuntimeError(f"vision timed out after {c['timeout_s']:.0f}s")
            time.sleep(0.4)
    finally:
        if proc.poll() is None:
            proc.kill()
        th.join(5)
    out = "\n".join(lines)
    text = _parse(out)
    if proc.returncode != 0 or not text:
        tail = "\n".join(lines[-15:])
        raise RuntimeError(f"vision failed (exit {proc.returncode}). Last output:\n{tail}")
    return {"text": text, "seconds": round(time.time() - t0, 1), "model": MODEL, "image": str(img)}
