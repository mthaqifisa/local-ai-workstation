# ADR-0001: LLM runtime — oMLX behind a Caddy LAN front door
Status: Accepted (2026-10-19). Re-evaluated by the P4 bake-off.

## Context
v5 was pinned to mlx-openai-server 1.8.1 (300 s prompt limit, vision hang, leaked caches). We need OpenAI **and** Anthropic APIs (Open WebUI, OpenCode, Claude Code), embeddings + rerank, VLM support, continuous batching for many devices, and memory-aware model eviction on 64 GB.

## Decision
- **oMLX** (Apache-2.0, Homebrew tap `jundot/omlx`) serves every MLX language/vision/embedding/rerank model from `~/LAW/models/llm` on `127.0.0.1:8001`, `--memory-guard-gb` from the registry budget, LRU eviction.
- oMLX refuses non-loopback binding without an API key. The user requires LAN access without login, so **Caddy** (Apache-2.0) listens on `0.0.0.0:8000` and reverse-proxies to oMLX, injecting the generated key. Clients on the LAN need no credentials.
- Fallback: `mlx_lm.server`/`mlx_vlm.server` behind llama-swap, behind the same Caddy port, selectable in `settings/registry.yaml`.

## Consequences
One endpoint (`http://<mac>:8000/v1`) for every client. Model swaps are delegated to oMLX; the LAW scheduler coordinates heavy media jobs by asking oMLX to unload.
