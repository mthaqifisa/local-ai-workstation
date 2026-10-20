# ADR-0002: Model roster (quality first, swap as needed)
Status: Accepted (2026-10-19). Revisions pinned in `settings/registry.yaml`.

| Role | Model (MLX) | Disk | Why |
|---|---|---|---|
| Reasoner / writer / vision (default) | lmstudio-community/Qwen3.8-27B-MLX-8bit | 29.5 GB | Strongest dense model that fits at 8-bit; native vision + tools |
| Coder | lmstudio-community/Qwen3-Coder-Next-MLX-4bit | 44.9 GB | Best open agentic coder that fits; challenged by Qwen3.8-27B in the P4 bake-off |
| Fast / orchestrator / vision | lmstudio-community/Qwen3.6-35B-A3B-MLX-8bit | 37.7 GB | MoE, 3B active: fast routing, summaries, screen reading |
| Independent judge | lmstudio-community/gemma-4-31B-it-MLX-8bit | 33.8 GB | Different family (Google) → no self-preference bias; 8-bit (upgraded from 6-bit 2026-10-19: fits the 48 GB budget) |
| Embeddings | mlx-community/Qwen3-Embedding-4B-4bit-DWQ | 2.3 GB | RAG |
| Reranker | mlx-community/Qwen3-Reranker-0.6B-4bit | 0.3 GB | RAG precision |

Rules: one heavy model resident (≤ 45 GB); light models (embed, rerank, STT, TTS) may co-reside. DeepSeek-R1 distills, Qwen3-VL-8B and Gemma-3 encoders are retired.

## Revision 2026-10-19 (after live testing)
- Judge upgraded 6-bit → 8-bit: the largest quant that fits; the judge's accuracy bounds the whole quality loop.
- Voice cloning: Chatterbox fp16 (full precision, non-turbo) instead of turbo 4-bit.
- Reasoning: Qwen3.8 runs with a per-role `reasoning_effort` (settings/roles.yaml). Unbounded thinking spent the
  full token budget on a simple finance question and returned nothing.
- Arithmetic: roles that handle numbers use the `run_python` calculator; the 27B model's mental arithmetic
  produced a wrong compound-interest figure that the judge also missed.
- Pending comparison on this Mac: ACE-Step 1.5 vs 1.5 XL (1.5 runs from a pinned mlx-audio branch; see ADR-0011); Wan 2.2 vs LTX-2.5 for video.

## Revision 2026-10-20: coder = Qwen3.8-27B (measured)
- Live test: a 16-minute OpenCode session building a Spring Boot + Angular app on Qwen3-Coder-Next was aborted by
  oMLX's memory guard: 41.4 GB weights + prefill working memory at ~38k tokens peaked at 47.1 GB (guard 46.6 GB).
- 8-bit KV cache (TurboQuant) gave no saving: Qwen3-Next uses linear attention in 37 of 48 layers; the peak is
  prefill activations, not KV.
- Decision: the `coder` role uses Qwen3.8-27B 8-bit (28 GB; SWE-bench Pro 61.7 on its card), leaving ~18 GB for long
  contexts. Qwen3-Coder-Next stays as `coder_fast` (32k context) for short tasks. Trade-off: ~10 vs ~55 tokens/s.
