"""Media handlers: image generation and editing, upscaling, TTS, voice cloning, STT, music.

Each shells out to a native MLX tool (mflux / mlx-audio) under ~/LAW/runtime/bin, writing into
~/LAW/outputs. They run on the queue's heavy or light lane per the registry, so a big image or
music job never fights a resident LLM for memory.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from law.handlers.base import Runtime, run_cmd
from law.queue.worker import Context, Done, Fatal, Outcome


def _out(rt: Runtime, kind: str, ext: str) -> Path:
    sub = {"image": "images", "edit": "images", "upscale": "images", "tts": "audio", "voice": "audio",
           "stt": "audio", "music": "audio", "video": "video", "animate": "video"}[kind]
    d = rt.paths.outputs / sub
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.{ext}"


DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?)\b", re.IGNORECASE)


def parse_duration(text: str, default: float, low: float, high: float) -> tuple[float, str]:
    """Pull a duration like '90s' or '2 min' out of a request; return (seconds within bounds, text without it)."""
    match = DURATION_RE.search(text)
    if not match:
        return default, text
    value = float(match.group(1)) * (60 if match.group(2).lower().startswith("m") else 1)
    rest = text[:match.start()] + text[match.end():]
    rest = re.sub(r"\s*,(\s*,)+", ",", rest)  # "beat, , rainy" -> "beat, rainy"
    rest = re.sub(r"\s{2,}", " ", rest).strip(" ,.-")
    return max(low, min(high, value)), rest


def split_lyrics(text: str) -> tuple[str, str | None]:
    """'style... lyrics: <lines>' -> (style, lyrics). Lyrics are kept exactly as written."""
    parts = re.split(r"(?im)^\s*lyrics\s*:\s*", text, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return text.strip(), None


ACE_MUSIC = Path(__file__).resolve().parents[1] / "tools" / "ace_music.py"  # runs in the acestep venv


def _bin(rt: Runtime, tool: str) -> str:
    return str(rt.paths.runtime / "bin" / tool)


def make_handlers(rt: Runtime) -> dict[str, Any]:
    async def image(ctx: Context) -> Outcome:
        spec = rt.reg.media["image"]
        p = ctx.job.payload
        out = _out(rt, "image", "png")
        cmd = [_bin(rt, spec.tool), "--model", spec.model, "--quantize", str(spec.quantize),
               "--steps", str(p.get("steps", spec.steps)), "--prompt", p["prompt"], "--output", str(out),
               "--width", str(p.get("width", 1024)), "--height", str(p.get("height", 1024))]
        if p.get("seed") is not None:
            cmd += ["--seed", str(p["seed"])]
        await ctx.progress(f"generating image with {spec.model}")
        await run_cmd(ctx, cmd, env={"HF_HOME": str(rt.paths.models / "hf")})
        if not out.exists():
            raise Fatal("image tool produced no file")
        return Done({"image": str(out)})

    async def edit(ctx: Context) -> Outcome:
        spec = rt.reg.media["edit"]
        p = ctx.job.payload
        sources = p.get("images") or ([p["image"]] if p.get("image") else [])
        if not sources:
            raise Fatal("edit needs an input image")
        for s in sources:
            if not Path(s).is_file():
                raise Fatal(f"no such image: {s}")
        out = _out(rt, "edit", "png")
        cmd = [_bin(rt, spec.tool), "--model", spec.model, "--quantize", str(spec.quantize),
               "--steps", str(p.get("steps", spec.steps)), "--prompt", p["prompt"],
               "--image-paths", *sources, "--output", str(out)]
        await ctx.progress("editing image")
        await run_cmd(ctx, cmd, env={"HF_HOME": str(rt.paths.models / "hf")})
        if not out.exists():
            raise Fatal("edit tool produced no file")
        return Done({"image": str(out), "sources": sources})

    async def upscale(ctx: Context) -> Outcome:
        spec = rt.reg.media["upscale"]
        p = ctx.job.payload
        if not Path(p["image"]).is_file():
            raise Fatal(f"no such image: {p['image']}")
        out = _out(rt, "upscale", "png")
        cmd = [_bin(rt, spec.tool), "--model", spec.model, "--image-path", p["image"], "--output", str(out)]
        await ctx.progress("upscaling")
        await run_cmd(ctx, cmd, env={"HF_HOME": str(rt.paths.models / "hf")})
        return Done({"image": str(out)})

    async def _speak(ctx: Context, kind: str) -> Outcome:
        spec = rt.reg.media[kind]
        p = ctx.job.payload
        out = _out(rt, kind, "wav")
        cmd = [_bin(rt, spec.tool), "--model", spec.model, "--text", p["text"],
               "--output_path", str(out.parent), "--file_prefix", out.stem, "--audio_format", "wav"]
        voice_name = p.get("voice") or spec.voice
        if voice_name:
            cmd += ["--voice", voice_name]
        if kind == "voice":
            ref = p.get("ref_audio")
            if not ref or not Path(ref).is_file():
                raise Fatal("voice cloning needs ref_audio (a sample of the voice)")
            cmd += ["--ref_audio", ref]
        await ctx.progress(f"synthesising speech with {spec.model}")
        await run_cmd(ctx, cmd, env={"HF_HOME": str(rt.paths.models / "hf")})
        produced = sorted(out.parent.glob(f"{out.stem}*.wav"))
        if not produced:
            raise Fatal("TTS produced no audio")
        return Done({"audio": str(produced[0])})

    async def tts(ctx: Context) -> Outcome:
        return await _speak(ctx, "tts")

    async def voice(ctx: Context) -> Outcome:
        return await _speak(ctx, "voice")

    async def stt(ctx: Context) -> Outcome:
        spec = rt.reg.media["stt"]
        p = ctx.job.payload
        if not Path(p["audio"]).is_file():
            raise Fatal(f"no such audio: {p['audio']}")
        fmt = p.get("format", "txt")
        stem = _out(rt, "stt", fmt).with_suffix("")  # the tool appends the extension itself
        cmd = [_bin(rt, spec.tool), "--model", spec.model, "--audio", p["audio"],
               "--output-path", str(stem), "--format", fmt]
        await ctx.progress("transcribing")
        await run_cmd(ctx, cmd, env={"HF_HOME": str(rt.paths.models / "hf")})
        produced = sorted(stem.parent.glob(f"{stem.name}*"))
        if not produced:
            raise Fatal("transcription produced no file")
        return Done({"text": produced[0].read_text().strip(), "file": str(produced[0])})

    async def music(ctx: Context) -> Outcome:
        spec = rt.reg.media["music"]
        p = ctx.job.payload
        caption, lyrics = split_lyrics(str(p.get("caption", p.get("prompt", ""))))
        seconds, caption = parse_duration(caption, float(p.get("duration", 60)), 15, 240)
        lyrics = p.get("lyrics") or lyrics or "[instrumental]"
        out = _out(rt, "music", "wav")
        cmd = [str(rt.paths.runtime / spec.tool / "bin" / "python"), str(ACE_MUSIC), "--model", spec.model,
               "--caption", caption, "--lyrics", lyrics, "--duration", str(int(seconds)), "--output", str(out)]
        if p.get("seed") is not None:
            cmd += ["--seed", str(p["seed"])]
        await ctx.progress("composing music")
        await run_cmd(ctx, cmd, env={"HF_HOME": str(rt.paths.models / "hf")}, timeout_s=3600)
        if not out.exists():
            raise Fatal("music tool produced no file")
        return Done({"audio": str(out)})

    async def _video(ctx: Context, kind: str) -> Outcome:
        spec = rt.reg.media[kind]
        p = ctx.job.payload
        out = _out(rt, kind, "mp4")
        seconds, prompt = parse_duration(str(p["prompt"]), (int(p.get("frames", 81)) - 1) / 16, 1, 10)
        explicit = "frames" in p and not DURATION_RE.search(str(p["prompt"]))
        frames = int(p["frames"]) if explicit else int(seconds * 16) + 1
        cmd = [_bin(rt, spec.tool), "generate", "--model", spec.model, "--prompt", prompt,
               "--steps", str(p.get("steps", spec.steps)), "--frames", str(frames),
               "--fps", str(p.get("fps", 16)), "--width", str(p.get("width", 832)),
               "--height", str(p.get("height", 480)), "--output", str(out)]
        if p.get("seed") is not None:
            cmd += ["--seed", str(p["seed"])]
        if kind == "animate":
            src = p.get("image")
            if not src or not Path(src).is_file():
                raise Fatal("animate needs an input image")
            cmd += ["--image-path", src]
        await ctx.progress(f"rendering video with {spec.model.split('/')[-1]} (this takes a while)")
        await run_cmd(ctx, cmd, env={"HF_HOME": str(rt.paths.models / "hf")}, timeout_s=4 * 3600)
        if not out.exists():
            raise Fatal("video tool produced no file")
        return Done({"video": str(out)})

    async def video(ctx: Context) -> Outcome:
        return await _video(ctx, "video")

    async def animate(ctx: Context) -> Outcome:
        return await _video(ctx, "animate")

    return {"image": image, "edit": edit, "upscale": upscale, "tts": tts, "voice": voice, "stt": stt,
            "music": music, "video": video, "animate": animate}
