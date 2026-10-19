# LAW v6 — Stack Research & Recommendation

> **Update 3 (2026-10-19), overrides anything below:** code only in `~/SourceCode` (released code in `v6/`); portable non-sensitive state (settings, knowledge, trained data) in `v6/settings`, `v6/knowledge`, `v6/trained`; everything machine-local (rendered config, secrets, models, history, media) in `~/LAW`. No secrets in v6. A fresh Mac with Homebrew + Python reproduces the setup via `git clone` + `./install.sh`, and the script is safe to re-run (PROMPT.md §1 rule 1, P3).

> **Update 2 (2026-10-19), overrides anything below:** Telegram is the primary channel; `law` CLI (as in v5) triggers agents; all services on 0.0.0.0 with no login for LAN devices; runtime agents get full machine access; v5's SQLite queue is removed and replaced by Valkey + a model-aware scheduler that swaps the best models as needed (PROMPT.md P4b). The CLI is named `law` (earlier drafts said `lai`).

*Format: consulting-style (Pyramid Principle). Research date: 19 Oct 2026. Hardware: MacBook Pro M5 Pro, 64 GB unified memory, 926 GB SSD.*
*Status: desk research. Every pick marked **(verify)** must be re-checked by the executing agent in Phase P1 before install.*

---

## 1. Executive summary

**Recommendation: assemble, don't build.** v4 and v5 spent most of their effort on custom dashboards, custom gateways and a custom Telegram bot, and those parts produced most of the defects. In October 2026, maintained open-source products cover every capability the user wants. v6 should therefore be a **thin, tested installer and evaluation layer** over these products:

1. **Inference:** **oMLX** (Apache-2.0) on MLX. It serves OpenAI- and Anthropic-compatible APIs, batches requests, and keeps an SSD-tiered KV cache, so Claude Code, OpenCode and Open WebUI can all use one local endpoint.
2. **Models (accuracy first):**
   - **Qwen3.8-27B** (dense, vision-language, 8-bit) for reasoning, judgement and writing;
   - **Qwen3-Coder-Next** for agentic coding;
   - **Qwen3.6-35B-A3B** for fast orchestration;
   - **Gemma 4 31B** (Apache-2.0) as an *independent judge from a different model family*.
3. **Hub:** **Open WebUI** for chat, RAG, tools/MCP, the code interpreter, voice and image hooks. **ComfyUI** is the media studio. **Homepage** gives one-glance service status.
4. **Media:** mflux and ComfyUI for images (FLUX.2-dev, Qwen-Image, Qwen-Image-Edit-2511); LTX-2.5 and Wan 2.2 for video; mlx-audio, Chatterbox and ACE-Step 1.5 XL for audio.
5. **Quality:** a generator → independent-judge loop, promptfoo/Inspect eval suites, and corporate-grade project templates with security scanning built in.

**Expected outcome:**
- one data root (`~/LAW`), so a reset is a single command;
- LAN-open service access (no login) for the user's devices, with Telegram allow-listed by user ID;
- a reproducible install with idempotence tests;
- quality measured by a judge from a different model family, not self-graded.

---

## 2. Situation, Complication, Resolution

| | |
|---|---|
| **Situation** | Two prior generations exist. v4 is an 8.5k-line bash installer. v5 is a Python `law` package with a reconcile engine and 201 sandboxed tests. Together they leave about 175 GB of models, caches and a VM on disk. |
| **Complication** | **Security:** v4 bound services to 0.0.0.0 with auth off and exposed the Docker socket on TCP 2375. **Quality:** v4's QA agent rewrote its own report from FAIL to 20/20. **Fragility:** v5 was pinned to an inference server with known hangs and a 300 s prompt limit. **Sprawl:** data lived in four roots. **Gaps:** no image editing, weak RAG, and CI that never ran. |
| **Resolution** | Wipe both stacks completely. Standardise on maintained open-source products, keep only a thin custom layer (installer, registry, evals, templates), enforce independent judging, and keep all data under `~/LAW`. |

---

## 3. Capability tree (MECE)

```
Local AI Workstation
├── A. Think        reasoning · judgement · computation · research (web + RAG)
├── B. Build        coding agents · SDLC templates · QA/security gates · docs
├── C. Create       image gen/edit · video gen/edit · audio (TTS/STT/clone/music/stems)
├── D. Assess       design critique (vision) · code security scanning
└── E. Operate      install · update · observe · back up · reclaim storage · uninstall
```

---

## 4. Evaluation criteria (weights)

| Criterion | Weight | Why |
|---|---|---|
| Output quality / accuracy | 35% | The user explicitly prioritises correctness over speed |
| Apple Silicon fitness (MLX/Metal) and 64 GB fit | 20% | Hard constraint |
| License: free, no subscription | 15% | Hard constraint (restrictive licenses are acceptable for personal use) |
| Maintenance health (releases ≤ 90 days, community) | 15% | Avoid v5-style lock-in to a stale component |
| Integration (OpenAI/Anthropic API, MCP, CLI) | 15% | Must plug into the hub and the coding agents |

Scores below are 1–5 against these criteria; **bold** marks the recommendation.

---

## 5. Domain findings

### 5.1 Inference runtime
| Option | Quality | Fit | License | Health | Integration | Weighted |
|---|---|---|---|---|---|---|
| **oMLX** | 5 | 5 | 5 (Apache-2.0) | 5 | 5 (OpenAI + Anthropic + embeddings + rerank) | **5.0** |
| mlx_lm.server + llama-swap | 5 | 5 | 5 | 4 | 3 (no Anthropic API; text only) | 4.5 |
| Ollama (MLX backend since 0.19) | 4 | 4 | 5 | 5 | 4 | 4.3 |
| LM Studio | 5 | 5 | 2 (closed-source app) | 5 | 4 | n/a, excluded |
| mlx-openai-server (used in v5) | 3 | 4 | 5 | 2 | 3 | Rejected |

**Notes:**
- oMLX was the only backend in one comparison that scaled under concurrent requests (1.3–1.4× from 1 to 4 requests).
- Its SSD-tiered KV cache cuts re-prefill time for coding agents.
- Its cold time-to-first-token is 2–3× slower than mlx_lm, which is acceptable because the user prioritises accuracy over speed.
- **(verify)** current release; vision support for Qwen3.8-27B; tool-call fidelity. **Plan B** is mlx_lm.server/mlx_vlm.server behind llama-swap. A bake-off in P4 decides.

### 5.2 Language models (64 GB budget: one large model resident; reserve ≥ 12 GB for macOS)
| Role | Recommendation | Size (approx.) | Evidence |
|---|---|---|---|
| Reasoner / writer / default | **Qwen3.8-27B, 8-bit MLX** (dense, vision-language, native tool calling) | ~30 GB | Model card: SWE-bench Pro 61.7, LiveCodeBench v6 90.3, Terminal-Bench 2.1 73.0 (vendor-reported) |
| Agentic coder | **Qwen3-Coder-Next 4-bit** (80B MoE, ~3B active) | ~42 GB | Top 64 GB coding pick in third-party rankings; P4 bake-off against Qwen3.8-27B-8bit decides |
| Fast orchestrator | **Qwen3.6-35B-A3B** (6- or 8-bit) | 28–37 GB | ~60–85 tok/s on M5 Pro |
| Independent judge | **Gemma 4 31B-it 4-bit** (Apache-2.0, released Apr 2026, 256K context) | ~18 GB | A different family from Qwen, which avoids self-preference bias |
| Embeddings / rerank | **Qwen3-Embedding-4B** + **Qwen3-Reranker-4B** | ~3–5 GB each | Strong MTEB results; served by oMLX `/v1/embeddings` and `/v1/rerank` |

**Retire:**
- DeepSeek-R1-Distill 32B/70B: obsolete, and the 70B crashed in both v4 and v5.
- Qwen3-VL-8B: Qwen3.8-27B is natively vision-language.
- Gemma-3 encoders: not needed unless the video stack requires them **(verify)**.

**Too big for 64 GB:** Nemotron 3 Super (64–72 GB at 4-bit), Qwen3.8-Flash-Next, GLM-5.x, DeepSeek-V4.

### 5.3 Hub / UI
| Option | Verdict |
|---|---|
| **Open WebUI** (v0.11.x) | **Pick.** Chat, RAG with citations, native MCP (Streamable HTTP; use `mcpo` for stdio servers), tools/functions, Jupyter code interpreter, STT/TTS, an image-generation backend (ComfyUI), and a mobile PWA. The branding clause only applies to deployments with more than 50 users. |
| LibreChat (MIT) | Strong multi-provider support, but weaker local media integration. Alternate. |
| AnythingLLM | Simpler, weaker for agents. |
| Custom dashboard (v4/v5) | **Retire.** Source of defects and drift. |

Service status: **Homepage** (gethomepage). Health checks: **Uptime Kuma** (optional).

### 5.4 Coding & SDLC
- **Agents:**
  - **OpenCode** (MIT; plan-first; local providers).
  - **Claude Code** pointed at oMLX's Anthropic-compatible endpoint, for offline or zero-cost runs.
  - Optional: Goose (Apache-2.0, now hosted by the Linux Foundation).
  - Aider is possibly dormant **(verify)**.
- **Quality gates:**
  - Lint and type checks: ruff, mypy/pyright, eslint/biome, shellcheck, hadolint.
  - Tests: pytest + coverage, Playwright, Lighthouse/axe.
  - Security scans: semgrep, gitleaks, trivy, osv-scanner.
  - Enforcement: pre-commit.
- **Templates:** `copier` templates, each with an ADR folder, threat-model section, CI workflow, test scaffold and a README following the house style.

### 5.5 Image generation & editing
| Model / tool | License | Use |
|---|---|---|
| **FLUX.2-dev** (32B; GGUF Q4–Q6 fits 64 GB) | Non-commercial (acceptable: personal use) | Highest photoreal quality; multi-reference |
| **Qwen-Image-2512** (20B) | Apache-2.0 | Best text rendering in images |
| **Qwen-Image-Edit-2511** | Apache-2.0 | Instruction editing, character consistency |
| FLUX.2 klein 4B | Apache-2.0 | Fast drafts |
| SeedVR2 (via mflux) | **(verify)** | Upscaling / restoration |
| **mflux** (MIT, MLX) | | Native CLI (generate, edit, fill, kontext, upscale) |
| **ComfyUI** (GPL-3.0, MPS) | | Node workflows; Open WebUI image backend |

### 5.6 Video generation & editing
| Option | License | Notes |
|---|---|---|
| **LTX-2.5 / LTX-2.3** via `ltx-2-mlx` | LTX license (free below US$10M revenue) | Text/image/audio-to-video with audio; native MLX; Q4 at ~12 GB peak |
| **Wan 2.2** (TI2V-5B, A14B) via `mlx-gen` or ComfyUI | Apache-2.0 | Text/image/video-to-video |
| ffmpeg, auto-editor, PySceneDetect | LGPL/GPL, MIT, BSD | Cut, trim, captions, scene splits |

### 5.7 Audio
| Need | Pick | License |
|---|---|---|
| TTS | **Kokoro-82M** via mlx-audio | Apache-2.0 |
| Voice cloning | **Chatterbox** (MPS) | MIT |
| STT | **Whisper-large-v3-turbo** (mlx-audio / mlx-whisper); Parakeet; Qwen3-ASR as alternates | MIT/Apache |
| Music | **ACE-Step 1.5 XL** (Apr 2026; LM + 4B DiT). The MLX port `mlx-music` is early, so run on PyTorch MPS | MIT |
| Stem separation | Demucs | MIT |

### 5.8 Computation & documentation
- **Computation:** Open WebUI code interpreter (Jupyter), JupyterLab, NumPy/SciPy/SymPy/pandas/Polars, DuckDB.
- **Documents:** pandoc, Typst, Marp (slides), Mermaid/Graphviz (diagrams), LibreOffice headless (Office round-trips), python-docx/python-pptx/openpyxl. House-style templates go in `templates/docs/`.

### 5.9 Security testing
Out of scope for v6 (removed at the user's request). Code security scanning (semgrep, gitleaks, trivy, osv-scanner) remains part of the SDLC quality gates.

### 5.10 Platform
- Containers: **Colima** + docker CLI + compose (OrbStack excluded: not open source).
- Observability (optional profile): **Langfuse** (MIT core) or Arize Phoenix.
- Evals: **promptfoo** (MIT; install from npm, not the pip wrapper), **Inspect AI** (MIT), DeepEval, Ragas.
- Channels: Telegram bot (primary; thin adapter on aiogram or python-telegram-bot), `law` CLI, Open WebUI on the LAN.
- Queue: **Valkey** (BSD-3) + a maintained job framework (Dramatiq / Taskiq / arq / Celery — pick in P1), with a small model-aware scheduler for memory admission and swap minimisation.

---

## 6. Memory budget (64 GB)

| Slot | Budget |
|---|---|
| macOS + apps reserve | 12 GB |
| Docker (Colima VM: Open WebUI, SearXNG, Homepage) | 6 GB |
| **One** resident large model (LLM *or* image *or* video) | ≤ 42 GB |
| KV cache / headroom | remainder (kv_bits 8; context sized per model) |

**Rules:**
- Only one large model is resident at a time; oMLX's LRU eviction, together with the `law` RAM guard, unloads the LLM before a media job starts.
- Raising the GPU wired limit with `sysctl iogpu.wired_limit_mb` needs sudo and the user's explicit consent.

---

## 7. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Model names and versions drift quickly | High | P1 re-verification; `registry.yaml` pins revisions plus checksums |
| oMLX regression or missing feature | Med | Plan B (mlx_lm + llama-swap) ready behind the same registry |
| OOM / kernel panic (seen in v4) | Med | RAM guard, one-model rule, context caps, soak test |
| Self-graded quality inflation (seen in v4) | High | Judge from a different model family; deterministic checks first, then the LLM judge |
| Accidental deletion during wipe | Med | Dry-run by default, explicit confirmation, archive before delete, ownership manifest |

## 8. Cost (TCO)
All software is free and open source or free to use. There are no subscriptions. The only cost is electricity and roughly 200–250 GB of disk for models.

## 9. Sources (accessed 19 Oct 2026)
- Qwen3.8-27B model card — https://huggingface.co/Qwen/Qwen3.8-27B
- Community MLX quants — https://huggingface.co/Chungulus/Qwen3.8-27B-MLX-8bit-Group32
- M5 Pro 64 GB benchmark — https://github.com/daniel29348679/m5pro-llm-bench
- Apple ML Research, MLX on M5 — https://machinelearning.apple.com/research/exploring-llms-mlx-m5
- Local server comparison — https://note.com/ryoji_hiwatari/n/n36cccc69484e?hl=en
- oMLX review — https://runaihome.com/blog/omlx-apple-silicon-llm-inference-review-2026/ ; https://www.promptquorum.com/power-local-llm/omlx-review
- 64 GB model roundup — https://modelgrep.com/best-local-llm/64gb-ram
- Open WebUI license — https://docs.openwebui.com/license/ ; MCP — https://docs.openwebui.com/features/extensibility/mcp
- Image models on Mac — https://medium.com/@michael.hannecke/the-best-local-ai-image-models-for-mac-in-2026-flux-vs-qwen-vs-sdxl-c68956c0cde0
- Video on Apple Silicon — https://github.com/james-see/ltx-video-mac ; https://github.com/lpalbou/mlx-gen
- Video model licensing — https://www.thundercompute.com/blog/best-open-source-ai-video-generation-models
- Audio — https://github.com/Blaizzy/mlx-audio ; https://www.yaps.ai/blog/best-local-tts-models
- ACE-Step 1.5 — https://github.com/ACE-Step/ACE-Step-1.5
- Gemma 4 — https://letsdatascience.com/news/google-releases-gemma-4-open-models-05c4c02a
- Coding agents — https://pinggy.io/blog/best_open_source_cli_coding_agents/ ; https://codemyspec.com/pages/cli-agents-compared-2026
- Eval frameworks — https://futureagi.com/blog/best-open-source-eval-frameworks-2026
