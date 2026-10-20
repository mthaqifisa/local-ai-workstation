"""Songs from a plain request: duration, theme, genre/vibe and optional lyrics.

A `song` job is a pipeline. When the user gave lyrics (a `lyrics:` section) or asked for an instrumental, it goes
straight to `music`. Otherwise the writer first plans a style caption and original lyrics for the theme
(`song_plan`), and `music` renders them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from law.handlers.base import Runtime
from law.handlers.core import answer
from law.handlers.media import parse_duration, split_lyrics
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

INSTRUMENTAL = re.compile(r"\b(instrumental|no vocals|without vocals|no lyrics)\b", re.IGNORECASE)

PLAN_PROMPT = """You are a songwriter. Plan a song for this request: {request}

It will be about {seconds} seconds long, so write about {lines} short singable lines in total.
Return only JSON with two keys:
- "caption": one line of comma-separated music tags: genre, mood, tempo in bpm, lead instruments, vocal type.
  Keep every style, genre, vibe and vocal choice the request gives; add sensible ones it leaves open.
- "lyrics": the lyrics with section tags on their own lines, like [verse], [chorus], [bridge], [outro]."""


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
        prompt = PLAN_PROMPT.format(request=p["request"], seconds=int(seconds), lines=max(4, int(seconds / 4)))
        await ctx.progress("writing lyrics")
        return Done(parse_plan(await answer(rt, "writer", [{"role": "user", "content": prompt}], ctx)))

    return {"song": song, "song_plan": song_plan}
