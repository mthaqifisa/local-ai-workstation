"""Songs and short films from a plain request: duration, theme, genre/vibe, and optional lyrics or script.

A `song` job is a pipeline. When the user gave lyrics (a `lyrics:` section) or asked for an instrumental, it goes
straight to `music`. Otherwise the writer first plans a style caption and original lyrics for the theme
(`song_plan`), and `music` renders them.

A `film` job is a pipeline too: the writer splits the request (or the user's `script:`) into shots of at most
5 seconds that add up to the duration (`film_plan`); each shot renders with the fast video profile; an
instrumental score is composed unless the user asks for no music; `film_join` cuts it together with ffmpeg.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from law.handlers.base import Runtime, run_cmd
from law.handlers.core import answer
from law.handlers.media import parse_duration, split_lyrics
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

SHOT_SECONDS = 5  # Wan 2.2 renders 81 frames (about 5 s at 16 fps) per clip
NO_MUSIC = re.compile(r"\b(no music|silent|without music)\b", re.IGNORECASE)

FILM_PROMPT = """You are a film director. Plan a {seconds}-second film for this request: {request}
{script}
Split it into exactly {shots} shots, in order. Each shot is one continuous camera take of at most {max_shot} seconds.
Return only JSON with two keys:
- "shots": a list of {shots} objects with "prompt" (a self-contained visual description for a video model:
  subject, action, setting, camera movement, lighting, style; repeat the look of recurring characters and places
  in every shot so they stay consistent) and "seconds" (a number from 2 to {max_shot}). The seconds add up to
  {seconds}.
- "music": one line of comma-separated tags for an instrumental score that fits the film (genre, mood, bpm,
  instruments)."""

INSTRUMENTAL = re.compile(r"\b(instrumental|no vocals|without vocals|no lyrics)\b", re.IGNORECASE)

PLAN_PROMPT = """You are a songwriter. Plan a song for this request: {request}

It will be about {seconds} seconds long, so write at most {lines} short singable lines in total (no more:
extra lines will not fit).
Return only JSON with two keys:
- "caption": one line of comma-separated music tags: genre, mood, tempo in bpm, lead instruments, vocal type.
  Keep every style, genre, vibe and vocal choice the request gives; add sensible ones it leaves open.
- "lyrics": the lyrics with section tags on their own lines, like [verse], [chorus], [bridge], [outro]."""


def lyric_lines(seconds: float) -> int:
    """About one sung line per 6 s after a 10 s intro. More lyrics make ACE-Step plan a longer song than asked,
    and the render then stops before the vocals arrive."""
    return max(4, int((seconds - 10) / 6))


def parse_plan(text: str) -> dict[str, str]:
    """The writer's JSON plan, tolerating a code fence or text around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise Fatal("the song plan was not JSON")
    try:
        plan = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise Fatal(f"the song plan was not valid JSON: {exc}") from exc
    caption, lyrics = str(plan.get("caption", "")).strip(), str(plan.get("lyrics", "")).strip()
    if not caption or not lyrics:
        raise Fatal("the song plan needs both a caption and lyrics")
    return {"caption": caption, "lyrics": lyrics}


def split_script(text: str) -> tuple[str, str | None]:
    """'style... script: <scenes>' -> (request, script)."""
    parts = re.split(r"(?im)^\s*script\s*:\s*", text, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return text.strip(), None


def parse_film(text: str, shots: int, seconds: float) -> dict[str, Any]:
    """The director's JSON plan, with shot lengths clamped and rescaled to fill the duration."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise Fatal("the film plan was not JSON")
    try:
        plan = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise Fatal(f"the film plan was not valid JSON: {exc}") from exc
    raw = [s for s in plan.get("shots", []) if str(s.get("prompt", "")).strip()]
    if len(raw) < shots:
        raise Fatal(f"the film plan has {len(raw)} usable shots, expected {shots}")
    raw = raw[:shots]
    lengths = [min(SHOT_SECONDS, max(2.0, float(s.get("seconds") or SHOT_SECONDS))) for s in raw]
    scale = seconds / sum(lengths)
    lengths = [round(min(SHOT_SECONDS, max(2.0, x * scale)), 2) for x in lengths]
    return {"shots": [{"prompt": str(s["prompt"]).strip(), "seconds": x} for s, x in zip(raw, lengths, strict=True)],
            "caption": str(plan.get("music", "")).strip() or "cinematic instrumental score"}


def make_handlers(rt: Runtime) -> dict[str, Any]:
    async def song(ctx: Context) -> Outcome:
        p = ctx.job.payload
        request, lyrics = split_lyrics(str(p.get("prompt", "")))
        seconds, request = parse_duration(request, float(p.get("duration", 60)), 15, 240)
        render: dict[str, Any] = {"duration": seconds, **({"seed": p["seed"]} if "seed" in p else {})}
        if lyrics or INSTRUMENTAL.search(request):
            return Stages([{"kind": "music", "payload": {**render, "caption": request,
                                                         "lyrics": lyrics or "[instrumental]"}}])
        return Stages([{"kind": "song_plan", "payload": {"request": request, "seconds": seconds}},
                       {"kind": "music", "payload": render}])

    async def song_plan(ctx: Context) -> Outcome:
        p = ctx.job.payload
        seconds = float(p["seconds"])
        prompt = PLAN_PROMPT.format(request=p["request"], seconds=int(seconds), lines=lyric_lines(seconds))
        await ctx.progress("writing lyrics")
        return Done(parse_plan(await answer(rt, "writer", [{"role": "user", "content": prompt}], ctx)))

    async def film(ctx: Context) -> Outcome:
        p = ctx.job.payload
        request, script = split_script(str(p.get("prompt", "")))
        seconds, request = parse_duration(request, float(p.get("duration", 20)), 4, 120)
        shots = math.ceil(seconds / SHOT_SECONDS)
        music = not NO_MUSIC.search(request) and p.get("music", True)
        plan = {"request": request, "script": script, "seconds": seconds, "shots": shots}
        specs: list[dict[str, Any]] = [{"kind": "film_plan", "payload": plan}]
        specs += [{"kind": "video", "payload": {"shot": i, **({"seed": p["seed"] + i} if "seed" in p else {})}}
                  for i in range(shots)]
        if music:
            specs.append({"kind": "music", "payload": {"duration": max(15.0, seconds), "lyrics": "[instrumental]"}})
        specs.append({"kind": "film_join", "payload": {"shots": shots, "music": bool(music)}})
        return Stages(specs)

    async def film_plan(ctx: Context) -> Outcome:
        p = ctx.job.payload
        script = f"Follow this script closely:\n{p['script']}\n" if p.get("script") else ""
        prompt = FILM_PROMPT.format(request=p["request"], script=script, seconds=int(p["seconds"]),
                                    shots=p["shots"], max_shot=SHOT_SECONDS)
        await ctx.progress(f"planning {p['shots']} shots")
        text = await answer(rt, "writer", [{"role": "user", "content": prompt}], ctx)
        return Done(parse_film(text, int(p["shots"]), float(p["seconds"])))

    async def film_join(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = p["_carry"]
        clips = [Path(carry[f"shot_{i}"]) for i in range(int(p["shots"]))]
        missing = [str(c) for c in clips if not c.is_file()]
        if missing:
            raise Fatal(f"missing shots: {missing}")
        out_dir = rt.paths.outputs / "video"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"film-{ctx.job.parent or ctx.job.id}.mp4"
        await ctx.progress(f"cutting {len(clips)} shots together")
        seconds = sum(float(shot["seconds"]) for shot in carry["shots"])
        await run_cmd(ctx, join_command(clips, Path(carry["audio"]) if p.get("music") else None, out, seconds))
        return Done({"video": str(out)})

    return {"song": song, "song_plan": song_plan, "film": film, "film_plan": film_plan, "film_join": film_join}


def join_command(clips: list[Path], music: Path | None, out: Path, seconds: float) -> list[str]:
    """ffmpeg: concatenate the shots (re-encoded, so any clip length works) and lay the score under them,
    trimmed to the picture with a 2-second fade-out."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    if music:
        cmd += ["-i", str(music)]
    chain = "".join(f"[{i}:v]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]"
    if music:
        fade = max(0.0, seconds - 2)
        chain += f";[{len(clips)}:a]atrim=0:{seconds:.2f},afade=t=out:st={fade:.2f}:d=2[a]"
    cmd += ["-filter_complex", chain, "-map", "[v]"]
    if music:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    return [*cmd, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", str(out)]
