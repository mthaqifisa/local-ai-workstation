# LAW v6 — Plan for producing the Claude Code build brief

> **Update 3 (2026-10-19), overrides anything below:** code only in `~/SourceCode` (released code in `v6/`); portable non-sensitive state (settings, knowledge, trained data) in `v6/settings`, `v6/knowledge`, `v6/trained`; everything machine-local (rendered config, secrets, models, history, media) in `~/LAW`. No secrets in v6. A fresh Mac with Homebrew + Python reproduces the setup via `git clone` + `./install.sh`, and the script is safe to re-run (PROMPT.md §1 rule 1, P3).

> **Update 2 (2026-10-19), overrides anything below:** Telegram is the primary channel; `law` CLI (as in v5) triggers agents; all services on 0.0.0.0 with no login for LAN devices; runtime agents get full machine access; v5's SQLite queue is removed and replaced by Valkey + a model-aware scheduler that swaps the best models as needed (PROMPT.md P4b). The CLI is named `law` (earlier drafts said `lai`).

## Context

> **Scope change (2026-10-19):** pen-testing dropped from v6 at the user's request.

`v6/idea.txt` asks for a **complete, very detailed brief** to hand to Claude Code. With it, Claude Code will:
- wipe the v4 and v5 local-AI setups (and older leftovers);
- build v6 from scratch on an M5 Pro with 64 GB, using only free and open-source tools, prioritizing accuracy over speed;
- cover coding, design, documentation, judgement, computation, and photo/video/audio editing and generation;
- include end-to-end tests good enough for a real SDLC, enforce bank/consultancy-grade output, research like a BCG team, choose the tech stack deliberately, and add a Solution Architect role;
- log progress so work can resume after the token budget runs out, and clean up storage.

**This session's deliverable is the brief and its supporting documents, written into `v6/`.** It does not build v6. The user will run the brief in Claude Code.

### Decisions already made by the user
- **Model cache:** wipe the whole Hugging Face cache (147 GB) and re-download only what v6 needs.
- **Folder layout:** all code go in `~/SourceCode`. Everything else (models, Docker data, outputs, logs, state, config) goes under one root, `~/LAW/`, split into subfolders, so a reset is a single `rm -rf ~/LAW`.
- **Delete:** `~/.codex`, `~/MLX-AI`, and every hidden folder left by v4/v5 or older stacks that v6 doesn't need.
- **Licenses:** choose the best quality regardless of license (personal use). Record each model's license in the registry anyway.

### What we learned from v4 and v5
- **v4:** an 8.5k-line bash installer that writes `~/.mlx-ai-workstation`. Problems:
  - a 4.4k-line inline dashboard;
  - binds to 0.0.0.0 with auth off, which makes remote shell possible over the LAN;
  - an unauthenticated Docker socket on port 2375;
  - persona drift across 4 sources;
  - the agent self-graded its QA (it rewrote its QA report from FAIL to 20/20);
  - out-of-memory kernel panics, fixed by a 32K context, kv_bits 8 and at most 6 tool calls per round;
  - the DeepSeek-R1-70B model was broken.
- **v5:** a Python package called `law` (`~/.law`) with a plan/apply reconcile engine, `owned.json`, a trash folder, fakeable system calls and 201 sandboxed tests.
  - Problems: locked to `mlx-openai-server` 1.8.1 and its quirks; a custom gateway, a 1.5k-line dashboard and a 1.6k-line Telegram monolith; no image-edit support; pure-Python RAG; CI never ran; four separate root folders.
- **Carry forward:** the registry as single source of truth, plan/apply with `--dry-run`, an ownership manifest, a sandboxed HOME in tests, a RAM guard, localhost-only binding, private-git undo, and v4's memory limits.
- **Main correction for v6:** stop building custom dashboards and gateways. Assemble maintained open-source products and keep custom code as a thin installer, config and evaluation layer.

### Live inventory to wipe (scanned 2026-10-19)

| Category | Items |
|---|---|
| Home folders | `~/.law` (2.1G), `~/.local-ai-workstation` (15M), `~/.colima` (9.6G), `~/ace-step` (11G), `~/MLX-AI` (12M), `~/.codex` (127M), `~/.cache/huggingface` (147G), `~/.cache/uv` (2G), `~/.local/share/uv` (1.7G; tools litellm and mflux), `~/Library/Caches/ms-playwright` (1G), `~/.config/opencode` |
| LaunchAgents | `com.law.{acestep,colima,dashboard,gateway,telegram,watchdog,worker}`, `com.aiws.mlx` (older stack), `ai.openclaw.gateway` (stale; `~/.openclaw` is already gone) |
| `~/.local/bin` | `law`, `lite`, `litellm`, `litellm-proxy`, `mflux-*` (about 40), broken `nano-pdf` symlink. **Keep `claude`.** |
| Brew packages that may have been installed by the stacks | colima, lima, docker, docker-compose, socat, `codexbar` (Codex), `openai-whisper`, `pytorch`. Use `owned.json` and the user's confirmation; never remove shared tools blindly (git, node, gh, ffmpeg, uv) |
| Also check | `~/.zprofile` lines added by v4/v5 (a backup already exists), `$TMPDIR/mlx-openai-prompt-cache-*`, `pytest-of-*`, `tmutil` local snapshots, `~/OneDrive/AI-Agent`, `~/.local-agent`, the VS Code extension `openai.codex-audio` (ask first) |
| Not ours — keep | `.continue`, `.copilot`, `.streamlit`, `.matplotlib`, `.claude*`, and other projects in `~/SourceCode` |
| Archive, then delete | the `v4/` and `v5/` source folders (88M): archive as `~/LAW/archive/v4-v5-<date>.tar.zst`, then delete, so the old personas and evals stay available for reference |

Disk: 359 GiB free of 926.

## Research synthesis (Oct 2026). The executing agent must re-verify each item in its Phase 1.

| Need | Pick (default) | Alternates / notes |
|---|---|---|
| LLM runtime | **oMLX** (MLX; batching; SSD KV cache; OpenAI **and** Anthropic APIs; on-demand models) | Fallback: `mlx_lm.server`/`mlx_vlm.server` behind **llama-swap**. A bake-off decides. Drop mlx-openai-server. |
| Primary reasoner/judge | **Qwen3.8-27B dense VL, 8-bit** (~30GB; per its card: SWE-bench Pro 61.7, native tools and vision) | 6-bit if KV headroom is needed |
| Agentic coder | **Qwen3-Coder-Next 80B-A3B 4-bit** (~42GB) vs Qwen3.8-27B 8-bit | Choose by local SWE-style eval |
| Fast/orchestrator | **Qwen3.6-35B-A3B** (MLX 8-bit if it fits, else 6-bit) | gpt-oss-20b |
| Cross-family judge | **Gemma 4 31B 4-bit** or gpt-oss-20b | Avoids the self-grading bias that inflated v4's QA |
| Embed + rerank | Qwen3-Embedding (4B/8B) + Qwen3-Reranker | |
| Chat / RAG / tools UI | **Open WebUI** (v0.11.x; native MCP; RAG; STT/TTS; image hooks; code interpreter) | Branding clause is fine for personal use |
| Web search | **SearXNG** | |
| Coding agents | **OpenCode** (MIT) + **Claude Code pointed at local Anthropic endpoint** | Goose optional |
| Image gen/edit | **ComfyUI** (desktop/manual on MPS) + **mflux** CLI: FLUX.2-dev (GGUF Q4–Q6), Qwen-Image-2512, **Qwen-Image-Edit-2511**, FLUX.2 klein | Upscale: SeedVR2 via mflux |
| Video gen/edit | **LTX-2.5 / LTX-2.3** (ltx-2-mlx), **Wan 2.2** (mlx-gen or ComfyUI) | ffmpeg, auto-editor, PySceneDetect |
| Audio | **mlx-audio** (Kokoro TTS, Whisper-large-v3-turbo / Parakeet / Qwen3-ASR STT), **Chatterbox** (cloning), **ACE-Step** (music; verify the latest version), Demucs (stems) | Stable Audio as alternate |
| Computation | Open WebUI code interpreter (Jupyter), JupyterLab, SymPy, NumPy/SciPy, DuckDB | |
| Docs | pandoc, Typst, Marp, Mermaid/Graphviz, LibreOffice headless; docx/pptx/xlsx Python libraries | |
| Design | Penpot (self-hosted, MPL) optional, Playwright screenshots, vision-model critique | |
| Containers | **Colima** + docker CLI + compose | OrbStack is not open source |
| Service dashboard | **Homepage** (gethomepage) with Open WebUI as the main hub | Uptime Kuma for health |
| Observability | **Langfuse** (self-host, MIT core) or Arize Phoenix | optional profile |
| Evals | **promptfoo** (npm), **Inspect AI**, DeepEval, Ragas, lm-eval-harness | |
| Quality gates | pre-commit, ruff, mypy/pyright, pytest+cov, Playwright, semgrep, gitleaks, trivy, osv-scanner, hadolint, shellcheck, Lighthouse/axe | |
| Phone access | Open WebUI PWA over **Tailscale** (free personal tier) instead of a custom Telegram bot | optional |

## Deliverables (written by this session, after plan approval)

1. **`v6/PLAN.md`**: a copy of this plan, for the user to review.
2. **`v6/RESEARCH.md`**: a BCG-style research report, Pyramid Principle structure:
   - executive summary, situation/complication/resolution, MECE capability tree;
   - per-domain options matrix (quality, license, Apple Silicon support, maintenance health, memory), weighted scoring, recommendation, risks and mitigations;
   - 64 GB memory budget, TCO (free), and sources with dates.
   - Uses the findings above plus further targeted web research during execution.
3. **`v6/PROMPT.md`**: **the master brief for Claude Code** (main artifact; structure below).
4. **`v6/CLAUDE.md`**: persistent project rules that Claude Code auto-loads in every session: paths, the progress protocol, quality bar, safety rules, never self-grade.
5. **`v6/PROGRESS.md`** + **`v6/progress.json`**: the resume log, seeded with "brief written, awaiting execution".
6. Leave `idea.txt` as is.

### `v6/PROMPT.md` structure (what Claude Code will execute)

- **§0 Mission and success criteria:** measurable definition of done per domain.
- **§1 Operating rules:**
  - Paths: code only in `~/SourceCode/OpenSourceLocalAI/v6`; non-code only under `~/LAW/{models/hf, models/comfy, docker, colima, outputs/{images,video,audio,docs}, rag, state, logs, backups, trash, archive, cache}`. Use env vars to point tools there: `HF_HOME`, `COLIMA_HOME`, `UV_CACHE_DIR`, `PLAYWRIGHT_BROWSERS_PATH`, `OLLAMA_MODELS` if used, and the ComfyUI `extra_model_paths`.
  - Progress protocol: update `PROGRESS.md` and `progress.json` after every task; at the start of every session, read the progress files and resume.
  - Commit per phase on a local git repo (branch `v6`).
  - Never self-grade.
  - Ask the user before sudo, before deleting brew packages, and at destructive gates.
  - Localhost binding only.
  - Run commands at most 10 minutes in the foreground; long downloads run in the background, logged.
  - On token exhaustion: the progress file must say exactly what to do next.
- **§2 Team and roles** (Claude Code subagents in `.claude/agents/`, mirrored as local OpenCode agents and Open WebUI model presets):
  - Engagement Lead/PM; **Solution Architect** (owns ADRs, the C4 diagrams, the tech-stack decision matrix and NFRs; signs off each phase); Research Analyst (BCG-style); BA/PO; Senior Developer; QA/SDET; Security Engineer (secure-by-design reviews, scanning); UX/Visual Designer; Technical Writer; Data/Computation Analyst; DevOps/SRE; and an **Independent Reviewer/Judge**, a separate model family with a rubric, who issues pass/fail.
  - Each role has a charter, inputs/outputs, a definition of done and a quality checklist (bank/consulting level).
- **§3 Phases, each with entry/exit gates and tests:**
  - **P0 Bootstrap:** progress files, `git init`, `CLAUDE.md`, preflight checks (macOS ≥ 26, arm64, RAM, free disk ≥ 250 GB), and a baseline disk report (`du` on every path in the inventory) saved to `~/LAW/logs/`.
  - **P1 Research and architecture (Research Analyst + Solution Architect):**
    - Re-verify every pick in the research table: latest versions, licenses, repo activity in the last 90 days, Apple Silicon support.
    - Write `docs/RESEARCH.md` updates, ADRs (`docs/adr/0001-…`), a C4 context/container diagram (Mermaid), NFRs (accuracy-first, localhost-only, single-root data, idempotent, resumable), and the 64 GB memory budget (one large model resident; macOS reserve ≥ 12 GB).
    - Gate: the Solution Architect and the Reviewer both sign off.
  - **P2 Inventory and wipe (destructive gate):**
    - Script: `scripts/wipe_legacy.sh`, with `--dry-run` as the default. It prints every path and its size, and asks for explicit confirmation before deleting anything.
    - Steps:
      1. Back up `.env`, personas and eval results to `~/LAW/archive/`.
      2. Unload and remove the legacy LaunchAgents.
      3. Stop and delete Colima, its VM, images and volumes.
      4. Remove the folders listed in the inventory, the `~/.local/bin` shims, uv tools and caches, the HF cache, the Playwright cache, `$TMPDIR` leftovers and `tmutil` local snapshots.
      5. Restore `~/.zprofile` (remove only the lines added by v4/v5).
      6. Archive, then delete, `v4/` and `v5/`.
      7. Ask per brew package that the stacks installed.
    - Verify: re-scan and confirm nothing remains, then record GB reclaimed in `PROGRESS.md`.
  - **P3 Installer:**
    - Layout: `install.sh` (bootstrap: Xcode CLT, Homebrew, uv), then `law` (Python 3.12 + Typer CLI, uv-locked).
    - Reuse v5's ideas: one `registry.yaml` as source of truth; plan → apply → verify with `--dry-run`; an `owned.json` manifest; idempotent re-runs; 7-day trash.
    - Use existing tools rather than writing new ones: `Brewfile` (brew bundle), `compose.yaml` (Colima: Open WebUI, SearXNG, Homepage, optional Langfuse), launchd plists generated from templates, and the `hf` CLI for downloads with checksum verification.
    - Commands: `law install|status|doctor|update|models|reclaim|backup|restore|uninstall`.
    - Every port binds to 127.0.0.1. Generated secrets go to `~/LAW/state/.env` with mode 0600.
  - **P4 Models and runtime:**
    - Bake-off: oMLX vs `mlx_lm.server` + llama-swap. Measure tool-call fidelity, vision, both the OpenAI and Anthropic APIs, cold-start time, peak RAM, and stability over a 2-hour soak. Record the result in an ADR.
    - Download the models in the registry. Apply v4's tuned limits (kv_bits 8, context sized to the RAM budget), plus a RAM guard and a watchdog.
  - **P5 Capabilities.** Each is wired into Open WebUI as model presets, tools or MCP, and into OpenCode/Claude Code:
    - coding (OpenCode, plus Claude Code on the local endpoint);
    - docs (pandoc/Typst/Marp templates in a corporate house style);
    - design (Playwright screenshots + vision critique);
    - computation (Jupyter code interpreter);
    - RAG (embeddings + reranker, PDF/Office ingestion);
    - web search (SearXNG);
    - image and video generation/editing (ComfyUI with saved workflows, mflux, LTX/Wan);
    - audio (mlx-audio, Chatterbox, ACE-Step, Demucs, ffmpeg);
  - **P6 Quality system:**
    - Role prompts and checklists.
    - A generator → independent judge loop: a different model family scores with a rubric, and work is never self-graded.
    - Project templates (cookiecutter/copier) with pre-commit, CI, test scaffolds, ADR, README and threat-model sections, so every project the local agents produce starts production-ready.
  - **P7 Testing (see below).**
  - **P8 Docs and handover:**
    - README, runbook, troubleshooting guide, architecture docs, a one-page quick start, and a test report.
    - Final storage cleanup: `law reclaim`, then a before/after disk report.
- **§4 Testing strategy:**
  - **Unit:** pytest with fakes for brew, launchctl, docker and hf, and a sandboxed HOME as in v5; coverage ≥ 85%.
  - **Integration:** real services. Health endpoints, port binding is localhost-only, idempotent re-install (second run = zero changes), uninstall/reinstall round trip, and a wipe dry-run.
  - **E2E UI:** Playwright on Open WebUI (chat, RAG upload and answer with citation, tool call, image request), ComfyUI (queue a saved workflow), and Homepage. Screenshots are saved to `~/LAW/outputs/test-artifacts`.
  - **Capability evals** with promptfoo/Inspect golden sets per domain, judged by the cross-family judge with thresholds:
    - **Coding:** generate a small app through the full SDLC; its own tests, lint and security scans must pass.
    - **Docs:** rubric scoring, plus pandoc builds without errors.
    - **Reasoning and computation:** answers with known solutions.
    - **Image:** a vision model checks the prompt was followed, plus a file/format check.
    - **Edit:** a before/after diff.
    - **Video:** frame count, duration and codec.
    - **TTS → STT round trip:** word error rate below a threshold.
    - **Music:** duration and a loudness check.
    - **Security tooling:** must find the documented issues in the local practice target.
  - **Non-functional:** soak test, peak RAM under the budget, and a cold-start SLO recorded.
  - **Gates:** `just test` (fast) / `just e2e` (full). A GitHub Actions workflow runs on a macOS runner for unit and lint. A results report goes to `docs/TEST-REPORT.md`.
- **§5 Resume protocol:**
  - At session start: read `CLAUDE.md`, `PROGRESS.md` and `progress.json`, then continue from the first unchecked task.
  - Each task records: status, files touched, commands run, and the next step.
- **§6 Definition of done:** every gate is green, the test report is attached, disk is reclaimed and reported, and the user's acceptance checklist is complete.

## Execution steps for this session (after approval)
1. Copy this plan to `v6/PLAN.md`.
2. Write `v6/RESEARCH.md` (BCG format), running a few more targeted web checks on items marked "verify".
3. Write `v6/PROMPT.md` in full (the §0–§6 content above, expanded with concrete commands, file trees, the registry schema and acceptance criteria).
4. Write `v6/CLAUDE.md` and seed `v6/PROGRESS.md` / `v6/progress.json`.
5. Tell the user how to run it: `cd ~/SourceCode/OpenSourceLocalAI/v6 && claude`, then "Execute PROMPT.md".

## Verification
- Every file exists in `v6/`, and nothing is written outside `v6/` except the plan file.
- `PROMPT.md` covers all 15 requirements in `idea.txt`. Write a traceability table at the end of `PROMPT.md` that maps each line of idea.txt to a section.
- Markdown renders; every link and path is valid.
