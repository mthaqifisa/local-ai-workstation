"""Song pipeline: plan parsing and routing (lyrics given, instrumental, or written by the writer)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from law.handlers import creative
from law.queue.worker import Fatal, Stages


def _song(payload: dict[str, Any]) -> Stages:
    handler = creative.make_handlers(SimpleNamespace())["song"]  # type: ignore[arg-type]
    out = asyncio.run(handler(SimpleNamespace(job=SimpleNamespace(payload=payload))))
    assert isinstance(out, Stages)
    return out


def test_parse_plan_accepts_fenced_json():
    plan = creative.parse_plan('Here:\n```json\n{"caption": "pop, 120 bpm", "lyrics": "[verse]\\nHi"}\n```')
    assert plan == {"caption": "pop, 120 bpm", "lyrics": "[verse]\nHi"}


@pytest.mark.parametrize("text", ["no json here", '{"caption": "pop"}', "{not json}"])
def test_parse_plan_rejects_bad_plans(text):
    with pytest.raises(Fatal):
        creative.parse_plan(text)


def test_theme_only_plans_lyrics_then_renders():
    out = _song({"prompt": "upbeat Malay pop about rainy KL nights, 2 min"})
    assert [s["kind"] for s in out.specs] == ["song_plan", "music"]
    assert out.specs[0]["payload"] == {"request": "upbeat Malay pop about rainy KL nights", "seconds": 120.0}
    assert out.specs[1]["payload"]["duration"] == 120.0


def test_user_lyrics_are_sung_as_written():
    out = _song({"prompt": "slow ballad, 90s\nlyrics:\n[verse]\nMy own words"})
    assert [s["kind"] for s in out.specs] == ["music"]
    assert out.specs[0]["payload"]["lyrics"] == "[verse]\nMy own words"
    assert out.specs[0]["payload"]["duration"] == 90.0


def test_instrumental_skips_lyric_writing():
    out = _song({"prompt": "lofi instrumental beat for studying", "seed": 3})
    assert [s["kind"] for s in out.specs] == ["music"]
    assert out.specs[0]["payload"]["lyrics"] == "[instrumental]" and out.specs[0]["payload"]["seed"] == 3


def test_lyric_length_fits_the_duration():
    assert creative.lyric_lines(15) == 4
    assert creative.lyric_lines(60) == 8
    assert creative.lyric_lines(240) == 38
