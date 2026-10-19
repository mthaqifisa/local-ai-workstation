# ADR-0004: Media stack
Status: Accepted (2026-10-19); exact model revisions in `settings/registry.yaml`.

- Images: **mflux** (MIT, MLX) CLI — FLUX.2, Qwen-Image, Qwen-Image-Edit, upscaling; **ComfyUI** (GPL-3.0, native MPS, `0.0.0.0:8188`) for node workflows and as the Open WebUI image backend.
- Video: **LTX-2.x MLX** and **Wan 2.2** via MLX runtimes; ffmpeg for editing.
- Audio: **mlx-audio** (Kokoro TTS, Whisper STT), Chatterbox (voice cloning), ACE-Step 1.5 (music), Demucs (stems).
- All media runs natively (Metal), never in Docker (no GPU in the VM). Every media job goes through the LAW queue so it never collides with a resident LLM.
