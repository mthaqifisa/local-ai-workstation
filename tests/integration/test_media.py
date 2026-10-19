"""Heavy media checks through the real queue. Slow (minutes each): run with `pytest -m "integration and heavy"`."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from law import client

pytestmark = [pytest.mark.integration, pytest.mark.heavy]

API = os.environ.get("LAW_SERVER", "http://127.0.0.1:8100")


def _run(kind: str, payload: dict[str, object]) -> dict[str, object]:
    job = client.run(API, kind, dict(payload), source="test")
    assert job["status"] == "done", job.get("error")
    result: dict[str, object] = job["result"]
    return result


def _probe(path: str) -> dict[str, object]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    data: dict[str, object] = json.loads(out)
    return data


def test_image_is_a_valid_png_of_requested_size() -> None:
    from PIL import Image

    path = str(_run("image", {"prompt": "A red bicycle against a white wall", "width": 768, "height": 512,
                              "seed": 7})["image"])
    with Image.open(path) as im:
        assert im.format == "PNG" and im.size == (768, 512)


def test_edit_changes_the_image() -> None:
    from PIL import Image, ImageChops

    src = str(_run("image", {"prompt": "A plain white mug on a wooden table", "width": 768, "height": 768,
                             "seed": 11})["image"])
    out = str(_run("edit", {"image": src, "prompt": "Make the mug bright red"})["image"])
    with Image.open(src) as a, Image.open(out) as b:
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB").resize(a.size))
        assert diff.getbbox() is not None  # something changed


def test_music_has_requested_duration() -> None:
    path = str(_run("music", {"caption": "calm acoustic guitar, warm, slow tempo", "duration": 20})["audio"])
    seconds = float(_probe(path)["format"]["duration"])  # type: ignore[index]
    assert 16 <= seconds <= 24


def test_video_has_frames_duration_and_codec() -> None:
    path = str(_run("video", {"prompt": "Waves rolling onto a beach at sunset", "frames": 33, "fps": 16,
                              "seed": 3})["video"])
    streams = _probe(path)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")  # type: ignore[union-attr,index]
    assert video["codec_name"] in {"h264", "hevc"}
    assert int(video["nb_frames"]) >= 30
    assert 1.5 <= float(video["duration"]) <= 3.0
