"""Local text-to-image generation (FLUX via mflux), as a queue job.

Image generation uses the GPU just like the language models, so it runs as a normal queue job
(kind "image") through the single worker — never at the same time as a chat/agent job. The
worker unloads any language model first (frees RAM/VRAM), then runs mflux's own CLI in the
model-server venv, streaming progress, and saves the PNG under ~/MLX-AI/outputs/images/.

No API keys, fully local. The FLUX weights download once on first use.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

DEFAULTS = {"model": "z-image-turbo", "width": 1024, "height": 1024, "timeout_s": 1800}
# Each image model has its OWN mflux command (this is how the proven original script did it —
# `mflux-generate --base-model X` only works for the FLUX variants and fails for the rest).
# Choice is research-backed (Mac blind tests, late 2026): z-image-turbo is the best speed/quality
# trade-off and Apache-2.0 (default); qwen is top "commercial" quality but slow (~6 min/image);
# flux2-klein is fast and Apache-2.0. All run locally with no API keys.
MODELS = {
    "z-image-turbo": {"cmd": "mflux-generate-z-image-turbo", "extra": [],                                 "steps": 9,  "note": "fast · Apache-2.0 · best balance (default)"},
    "z-image":       {"cmd": "mflux-generate-z-image",       "extra": [],                                 "steps": 20, "note": "photoreal · Apache-2.0"},
    "qwen":          {"cmd": "mflux-generate-qwen",          "extra": ["--quantize", "4"],                "steps": 20, "note": "top quality · slow (~6 min)"},
    "flux2":         {"cmd": "mflux-generate-flux2",         "extra": ["--base-model", "flux2-klein-4b", "--quantize", "4"], "steps": 20, "note": "FLUX.2 · fast · Apache-2.0"},
    "fibo":          {"cmd": "mflux-generate-fibo",          "extra": ["--quantize", "4"],                "steps": 20, "note": "FIBO"},
    "schnell":       {"cmd": "mflux-generate",               "extra": ["--base-model", "schnell", "--quantize", "4"], "steps": 4, "note": "FLUX.1-schnell"},
}


def exe(paths, model: str = "qwen") -> Path | None:
    cmd = MODELS.get(model, MODELS["qwen"])["cmd"]
    p = paths.runtime / ".venv" / "bin" / cmd
    return p if p.exists() else None


def available(paths) -> bool:
    return any((paths.runtime / ".venv" / "bin" / spec["cmd"]).exists() for spec in MODELS.values())


def out_dir(paths) -> Path:
    d = paths.outputs / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate(paths, prompt: str, *, out: Path | None = None, cfg: dict | None = None,
             seed: int | None = None, touch=None, check=None, on_log=None) -> dict:
    """Run mflux to make one image. Returns {path, seconds, model}. Raises RuntimeError on failure."""
    c = {**DEFAULTS, **(cfg or {})}
    sel = c.get("model") or c.get("base_model")      # accept either key
    model = sel if sel in MODELS else "z-image-turbo"
    spec = MODELS[model]
    tool = exe(paths, model)
    if tool is None:
        raise RuntimeError(f"image model '{model}' command ({spec['cmd']}) not found in the model-server venv")
    prompt = (prompt or "").strip()
    if not prompt:
        raise RuntimeError("empty prompt")
    steps = int(c.get("steps") or spec["steps"])
    # These diffusion models produce NOISE below their native size and need dimensions that are
    # multiples of 64. Clamp to a safe minimum of 1024 (verified: z-image-turbo is garbage at 768,
    # excellent at 1024) and snap to a multiple of 64 so a bad size can't silently ruin the output.
    def _dim(v):
        return max(1024, (int(v) // 64) * 64)
    c["width"], c["height"] = _dim(c["width"]), _dim(c["height"])
    dest = Path(out) if out else (out_dir(paths) / f"img-{int(time.time())}.png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [str(tool), *spec["extra"], "--prompt", prompt, "--steps", str(steps),
            "--width", str(c["width"]), "--height", str(c["height"]), "--output", str(dest)]
    neg = (c.get("negative_prompt") or "").strip()
    if neg:
        args += ["--negative-prompt", neg]
    if c.get("guidance") is not None:
        args += ["--guidance", str(c["guidance"])]
    if seed is not None:
        args += ["--seed", str(seed)]
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
                check()                           # raises Cancelled/Interrupted
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError(f"image generation timed out after {c['timeout_s']:.0f}s")
            time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.kill()
        th.join(5)
    if proc.returncode != 0 or not dest.exists():
        tail = "\n".join(lines[-12:])
        raise RuntimeError(f"image generation failed (exit {proc.returncode}). Last output:\n{tail}")
    return {"path": str(dest), "seconds": round(time.time() - t0, 1),
            "model": model, "bytes": dest.stat().st_size}
