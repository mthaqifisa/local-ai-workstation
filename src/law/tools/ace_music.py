"""Generate a song with ACE-Step 1.5 (MLX). Runs inside the `acestep` tool venv, not law's venv.

ACE-Step lives on an unreleased mlx-audio branch, so it gets its own pinned venv (see settings/registry.yaml)
and this thin entry point. It imports nothing from law.
"""

from __future__ import annotations

import argparse

import mlx.core as mx
import numpy as np
import soundfile as sf
from mlx_audio.tts import load

PEAK = 10 ** (-1 / 20)  # -1 dBFS headroom


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--caption", required=True, help="style, genre, mood, instruments, vocal type")
    ap.add_argument("--lyrics", default="[instrumental]")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--language", default="en")
    ap.add_argument("--lm-size", default="4B", choices=["0.6B", "4B"])
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()

    model = load(a.model)
    chunks, rate = [], 48000
    for result in model.generate(text=a.caption, lyrics=a.lyrics, duration=a.duration, vocal_language=a.language,
                                 lm_model_size=a.lm_size, num_steps=a.steps, seed=a.seed):
        chunks.append(np.array(result.audio.astype(mx.float32)))
        rate = result.sample_rate
    if not chunks:
        raise SystemExit("ACE-Step returned no audio")
    audio = np.concatenate(chunks)
    peak = float(np.max(np.abs(audio)))
    if peak > PEAK:  # scale down to -1 dBFS instead of hard-clipping the loudest moments
        audio = audio * (PEAK / peak)
    sf.write(a.output, audio, rate, subtype="PCM_16")


if __name__ == "__main__":
    main()
