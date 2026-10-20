# ADR-0012: Fast video (Wan 2.2 Lightning) and multi-shot films
Status: Accepted (2026-10-20)

## Context
Wan 2.2 A14B (8-bit, 40 steps) takes about 44 minutes on this Mac for one 2-second 832x480 clip (two measured
runs: 2,660 s and 2,712 s for 33 frames). Films of a user-chosen length need many shots, so at 40 steps a
30-second film would take more than half a day.

## Decision
- Each video model in the registry gets a `fast` profile: the lightx2v **Wan2.2-Lightning** 4-step LoRA pair
  (high-noise and low-noise; Seko V1.1 for text-to-video, Seko V1 for image-to-video), guidance 1, flow-shift 5.
  mlx-gen 0.38 lists this exact combination on these q8 models as LoRA-validated
  (`lora_wan_a14b_q8_lightx2v_4step_t2v_2026_06_12`). Only the four adapter files are pre-downloaded, pinned.
- `film` jobs: the writer plans shots of at most 5 s that add up to the requested duration (4–120 s), from
  the request or the user's `script:`; every shot renders on the fast profile; ACE-Step composes an
  instrumental score; ffmpeg concatenates and mixes with a 2 s fade-out.
- Single `/video` clips keep the 40-step quality profile by default.

## Consequences
- Distilled 4-step output is somewhat less detailed than 40 steps; films trade that for practical render times.
- Shots are generated independently, so characters can drift between shots. The planner repeats each
  character's and place's description in every shot prompt to limit this.
- Speed on this Mac is measured by the first film render and recorded in PROGRESS.md.
