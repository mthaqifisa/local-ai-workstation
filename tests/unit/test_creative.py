"""Song pipeline: plan parsing and routing (lyrics given, instrumental, or written by the writer)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from law.handlers import creative
from law.queue.worker import Fatal, Stages

REPO = Path(__file__).resolve().parents[2]


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


def test_parse_film_rescales_shots_to_fill_the_duration():
    text = ('{"shots": [{"prompt": "a", "seconds": 5}, {"prompt": "b", "seconds": 5}, {"prompt": "c", "seconds": 1}],'
            ' "music": "ambient, 70 bpm"}')
    plan = creative.parse_film(text, shots=3, seconds=12)
    lengths = [s["seconds"] for s in plan["shots"]]
    assert all(2 <= x <= creative.SHOT_SECONDS for x in lengths) and abs(sum(lengths) - 12) < 0.5
    assert plan["caption"] == "ambient, 70 bpm"


def test_parse_film_rejects_too_few_shots():
    with pytest.raises(Fatal, match="expected 3"):
        creative.parse_film('{"shots": [{"prompt": "a", "seconds": 5}]}', shots=3, seconds=15)


def _film(payload: dict[str, Any]) -> Stages:
    handler = creative.make_handlers(SimpleNamespace())["film"]  # type: ignore[arg-type]
    out = asyncio.run(handler(SimpleNamespace(job=SimpleNamespace(payload=payload))))
    assert isinstance(out, Stages)
    return out


def test_film_plans_one_render_per_five_seconds_then_scores_and_cuts():
    out = _film({"prompt": "30s, a lone astronaut finds a garden on Mars, cinematic", "seed": 10})
    kinds = [s["kind"] for s in out.specs]
    assert kinds == ["film_plan"] + ["video"] * 6 + ["music", "film_join"]
    assert out.specs[0]["payload"]["shots"] == 6 and out.specs[0]["payload"]["seconds"] == 30
    assert [s["payload"]["seed"] for s in out.specs[1:7]] == list(range(10, 16))


def test_film_without_music_and_with_a_script():
    out = _film({"prompt": "10s, silent noir mood\nscript:\nA detective lights a match.\nRain on the window."})
    assert [s["kind"] for s in out.specs] == ["film_plan", "video", "video", "film_join"]
    assert out.specs[0]["payload"]["script"] == "A detective lights a match.\nRain on the window."
    assert out.specs[-1]["payload"]["music"] is False


def test_wan_frames_are_4k_plus_1():
    from law.handlers.media import wan_frames

    assert [wan_frames(s) for s in (5, 3.3, 2, 0.2)] == [81, 53, 33, 17]


def test_join_command_cuts_clips_and_lays_the_score(tmp_path):
    import json as _json
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    clips = []
    for i, colour in enumerate(("red", "green", "blue")):
        clip = tmp_path / f"shot{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color={colour}:s=160x96:r=16:d=1.5",
                        "-pix_fmt", "yuv420p", str(clip)], check=True)
        clips.append(clip)
    music = tmp_path / "score.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=15",
                    str(music)], check=True)
    out = tmp_path / "film.mp4"
    subprocess.run(creative.join_command(clips, music, out, 4.5), check=True)
    info = _json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                       str(out)], capture_output=True, text=True, check=True).stdout)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - 4.5) < 0.3


def test_film_shot_renders_with_the_fast_lightning_profile(tmp_path, monkeypatch):
    from pathlib import Path

    from law import registry
    from law.handlers import media
    from law.paths import LawPaths

    repo = Path(__file__).resolve().parents[2]
    paths = LawPaths(repo=repo, home=tmp_path / "LAW", user_home=tmp_path)
    reg = registry.load(paths.registry_file)
    seen: list[list[str]] = []

    async def fake_run_cmd(ctx, cmd, **kw):  # type: ignore[no-untyped-def]
        seen.append(cmd)
        Path(cmd[cmd.index("--output") + 1]).write_bytes(b"mp4")  # noqa: ASYNC240 - test fake
        return ""

    async def progress(_: str) -> None:
        return None

    monkeypatch.setattr(media, "run_cmd", fake_run_cmd)
    rt = SimpleNamespace(reg=reg, paths=paths)
    video = media.make_handlers(rt)["video"]  # type: ignore[arg-type]
    carry = {"shots": [{"prompt": "astronaut kneels by a flower", "seconds": 3.3}]}
    ctx = SimpleNamespace(job=SimpleNamespace(payload={"shot": 0, "_carry": carry}), progress=progress)
    result = asyncio.run(video(ctx)).result
    cmd = seen[0]
    assert set(result) == {"shot_0"}
    assert cmd[cmd.index("--steps") + 1] == "4" and cmd[cmd.index("--frames") + 1] == "53"
    assert cmd[cmd.index("--flow-shift") + 1] == "5.0"
    roles = cmd[cmd.index("--lora-target-roles") + 1:]
    assert roles[:2] == ["high_noise_transformer", "low_noise_transformer"]
    loras = cmd[cmd.index("--lora-paths") + 1:cmd.index("--lora-target-roles")]
    assert len(loras) == 2 and all(x.startswith("lightx2v/Wan2.2-Lightning:Wan2.2-T2V") for x in loras)
    assert "@" not in " ".join(loras)  # not downloaded in this sandbox: the repo:path fallback, without the pin


def test_fast_video_passes_the_downloaded_lora_files(tmp_path):
    from law import registry

    reg = registry.load(REPO / "settings" / "registry.yaml")
    fast = reg.media["video"].fast
    assert fast is not None
    hf = tmp_path / "hf"
    for lora in fast.loras:
        ref, path = lora.split(":", 1)
        repo, _, rev = ref.partition("@")
        f = hf / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / rev / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"lora")
    args = fast.lora_args(hf)
    assert all(Path(a).is_file() and "18bccf88" in a and a.endswith(".safetensors") for a in args)
