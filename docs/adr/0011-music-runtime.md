# ADR-0011: Music runs ACE-Step 1.5 from a pinned mlx-audio branch
Status: Accepted (2026-10-20)

## Context
ADR-0004 picked ACE-Step 1.5 for music (songs with vocals and instruments). The weights we use,
`mlx-community/ACE-Step1.5-MLX`, were converted with mlx-audio, but no mlx-audio release (0.4.x through 0.6.0)
contains the ACE-Step model. It lives only on the `pc/add-ace` branch behind the still-open PR #499, with the vocal
and weight-conversion fixes from PR #653 merged into it. The pinned release 0.5.9 failed every music job with
"Model type acestep not supported for music".

## Decision
- A separate tool venv, `~/LAW/runtime/acestep`, installs mlx-audio from that branch at commit `1e8264a4`. It is
  separate so the TTS/STT tools keep their released, newer mlx-audio.
- `src/law/tools/ace_music.py` is a thin entry point that runs inside that venv: caption, lyrics, duration and seed
  in, a 48 kHz stereo WAV out.
- The turbo model needs its 5 Hz planner LM. We use the **4B** planner (best structure and vocals; 8.4 GB),
  pre-downloaded at a pinned revision via `hf_extra: [repo@revision]`.

## Consequences
- Music works today without waiting for an upstream release.
- The branch is from April 2026 and may lag fixes. When an mlx-audio release ships ACE-Step, move to it, drop the
  separate venv, and re-run the music integration test.
- Peak memory is estimated at about 24 GB (DiT, VAE and the 4B planner; measure on first runs), so music runs on the heavy lane alone.
