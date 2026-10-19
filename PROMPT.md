# LAW v6 — Master Build Brief for Claude Code

> **How to use:** `cd ~/SourceCode/OpenSourceLocalAI/v6 && claude`, then say: **"Execute PROMPT.md. Follow CLAUDE.md. Resume from PROGRESS.md."**
> This brief is self-contained. Supporting documents in this folder: `PLAN.md` (context and inventory), `RESEARCH.md` (stack research), `CLAUDE.md` (standing rules), `PROGRESS.md` / `progress.json` (resume log).

---

## §0 Mission

Build **LAW v6**, a local, fully free and open-source AI workstation on a **MacBook Pro M5 Pro / 64 GB**. It must produce **bank- and consultancy-grade** output for:
- coding;
- design;
- documentation;
- judgement and reasoning;
- computation;
- photo, video and audio editing and generation.

Accuracy, correctness and quality take priority over speed.

Before building, **completely remove v4, v5 and older leftovers**, and reclaim storage.

### Success criteria (all must be true)
1. **Clean wipe:** a re-scan finds no v4/v5/older artifacts from the `PLAN.md` inventory, and the reclaimed GB are recorded.
2. **One-command install:** `./install.sh` on this Mac reaches a healthy state.
3. **Idempotent:** a second `law install` reports **0 changes**.
4. **Healthy:** `law doctor` is all green.
5. **Clean filesystem:**
   - code lives only in `~/SourceCode` (released v6 code in `v6/`), and v6 also holds the portable, non-sensitive state (`settings/`, `knowledge/`, `trained/`);
   - every machine-local file (rendered config, secrets, models, history, media, caches) lives only under `~/LAW/`;
   - no secret is ever committed to v6 (gitleaks clean on the full history);
   - nothing else in `$HOME` changes, except the documented, reversible shell-profile lines and LaunchAgents (both listed in `owned.json`).
6. **LAN-open:** every service binds to `0.0.0.0` with no login, and is reachable from the user's other devices on the same network. A test from the LAN IP proves it.
7. **Every capability passes** its E2E eval in §4, judged by the **independent judge** (a different model family from the generator) plus deterministic checks.
8. **Docs complete:** README, RUNBOOK, ARCHITECTURE (C4), ADRs, TEST-REPORT and a quick-start guide.
9. **Uninstall works:** `law uninstall` followed by `law install` round-trips cleanly. `rm -rf ~/LAW` plus `law uninstall --system` leaves the Mac clean.
10. **Reproducible and re-runnable:** on a freshly formatted Mac with only Homebrew and Python, `git clone` + `./install.sh` reproduces the same setup (settings, roles, knowledge, trained data). Running the script any number of times is safe and converges (§3 P3 re-run guarantees).
11. **Telegram and CLI work:** every capability can be triggered from Telegram and from the `law` CLI, and jobs run through the v6 queue (§3 P4b).

---

## §1 Operating rules (non-negotiable)

1. **Paths and data placement (user decision, 2026-10-19).**
   - **Code** lives only in `~/SourceCode`. All **released** v6 code lives in `~/SourceCode/OpenSourceLocalAI/v6`. Code projects created by the runtime agents go to `~/SourceCode/<project>/`.
   - **v6 is the portable source of truth.** A new or freshly formatted Mac must get the *same setup* with `git clone` + `./install.sh`. So v6 (git) also holds every important, **non-sensitive** piece of state:
     ```
     v6/
       settings/           # registry.yaml (models, services, ports, licenses, revisions), roles.yaml (personas/agents),
                           # openwebui/ (exported presets, prompts, tools, knowledge-collection definitions),
                           # comfy/workflows/, homepage/, telegram.yaml (no tokens), law.yaml (preferences),
                           # legacy_inventory.yaml
       knowledge/          # RAG source documents, curated agent long-term memory (exported, sanitized), glossaries
       trained/            # LoRAs, the user's own voice reference clips, fine-tune datasets, eval golden sets + baselines (Git LFS)
       templates/          # house-style document templates, copier project templates, launchd templates
     ```
   - **`~/LAW/` holds everything machine-local and disposable** (losing it on a format is acceptable):
     ```
     ~/LAW/
       config/             # runtime config rendered from v6/settings + machine overrides (never edited by hand)
       state/              # .env (0600: Telegram token, allowed user IDs, HF token, service keys), owned.json, install.json
       data/               # derived data rebuilt from v6: vector indexes, embeddings, service DBs, job history
       models/{hf,comfy,other}/   # HF_HOME etc.
       runtime/            # uv-managed venvs: control, media, comfyui, acestep
       colima/  docker/    # COLIMA_HOME, bind-mounted service volumes (Open WebUI, SearXNG, Homepage, Valkey, Langfuse)
       outputs/{images,video,audio,docs,test-artifacts}   # generated media
       logs/  backups/  trash/  archive/  cache/{uv,pip,npm,playwright,torch,tmp}
     ```
   - **No secrets in v6, ever.** Tokens, keys, passwords and credentials live only in `~/LAW/state/.env`. gitleaks plus a custom pattern check run in pre-commit, in CI and inside `law save`; any hit aborts the commit.
   - Point every tool at `~/LAW` through environment variables set in one generated file, `~/LAW/config/env.sh`, which `~/.zprofile` sources via **one marked line**:
     `HF_HOME`, `HF_HUB_CACHE`, `COLIMA_HOME`, `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR`, `PIP_CACHE_DIR`, `npm_config_cache`, `PLAYWRIGHT_BROWSERS_PATH`, `TORCH_HOME`, `XDG_CACHE_HOME` for the tools that honour it.
2. **Progress logging.**
   - After **every** task, update `PROGRESS.md` (a human checklist plus a dated log line) and `progress.json`: `{phase, task, status, files_touched, commands, next_step, timestamp}`.
   - Commit to local git after each completed task, on branch `v6`, using conventional commits.
   - If the session might end, write the exact next command into `next_step` first.
3. **Resume.** At session start, always read `CLAUDE.md`, then `PROGRESS.md` and `progress.json`, and continue from the first unchecked task. Never redo completed work unless a check fails.
4. **Ask before:**
   - `sudo`;
   - deleting anything outside the `PLAN.md` inventory;
   - removing brew packages;
   - any destructive gate (P2);
   - spending more than 50 GB of downloads in one step.
   Use a clear yes/no question.
5. **Never self-grade.** A deliverable passes only on deterministic checks (tests, linters, scanners, file probes) **and** a verdict from the independent judge model or Claude reviewer subagent. Reports are generated from raw results and never hand-edited.
6. **Prefer existing tools over writing new ones.** Write custom code only for glue: the installer, registry, evals and templates. No custom chat UI, gateway or dashboard.
7. **Network and access (user decision, 2026-10-19).**
   - All services bind to `0.0.0.0` and are open to the LAN with **no login** (e.g. Open WebUI `WEBUI_AUTH=False`). The Mac is the AI server for the user's other devices.
   - Runtime AI agents have **full access to the machine** as the user's account: all tool permissions set to allow, no approval prompts, no command block-lists. Private-git undo snapshots are kept purely as a recovery aid. This explicitly includes **controlling the Mac like a person would**: opening and using any app, browsing, downloading and installing software, playing media, changing system settings such as volume. See P4b "Computer use".
   - The one exception is Telegram, which is reachable from the internet: the bot answers only the Telegram user IDs listed in `~/LAW/state/.env`. This is an allow-list, not a login.
   - The Docker socket is not exposed over TCP (nothing needs it).
   - Secrets (bot token, HF token) live in `~/LAW/state/.env` (0600) and stay out of git (gitleaks runs in pre-commit).
   - Rule 4 ("Ask before") applies to **this build session** only, not to the runtime agents.
8. **Long-running work.**
   - Foreground commands time out after 10 minutes. Run downloads, model loads and soak tests in the background with logs written to `~/LAW/logs/`.
   - Verify downloads with checksums or file sizes.
9. **Research honesty.**
   - Before installing any component, confirm its current version, license and recent activity.
   - If a pick in `RESEARCH.md` is outdated or broken, choose the best current alternative and record why in an ADR.
10. **Quality bar.** Every file you write must be production-grade:
    - typed;
    - documented;
    - tested;
    - linted;
    - free of TODOs;
    - free of dead code.

---

## §2 Team & roles

Create each role as a Claude Code subagent in `v6/.claude/agents/<role>.md`. Mirror the same charters as **local** personas for OpenCode (`opencode.json` agents) and as Open WebUI model presets with system prompts. Keep the personas in one source of truth, `settings/roles.yaml`, and generate all three forms from it (v4 suffered persona drift across four copies).

| Role | Charter | Key outputs | Definition of done |
|---|---|---|---|
| **Engagement Lead / PM** | Plans, sequences and tracks; owns PROGRESS | Phase plan, status, risk log | Every task has an owner, a gate and evidence |
| **Solution Architect** | Owns the architecture and tech-stack decisions; signs off every phase gate | ADRs, C4 diagrams (Mermaid), NFRs, decision matrices, memory budget, integration contracts | Each choice is backed by an ADR and evidence from a scored alternatives table; NFRs are traceable to tests |
| **Research Analyst** (BCG-style) | Hypothesis-driven research; Pyramid Principle; MECE | Updates to RESEARCH.md with dated sources | Every claim is sourced; recommendation first |
| **BA / Product Owner** | Requirements and acceptance criteria | User stories plus Gherkin acceptance tests per capability | Every capability has testable AC |
| **Senior Developer** | Installer, CLI, glue | Typed Python 3.12, shell (shellcheck-clean) | Tests ≥ 85% coverage; lint and type checks clean |
| **QA / SDET** | Test strategy and automation | Unit, integration, E2E and eval suites; TEST-REPORT | All gates green; no flaky tests (3 consecutive green runs) |
| **Security Engineer** | Threat model, hardening, code/dependency scanning | STRIDE threat model, scan reports | No high/critical code findings open; LAN exposure documented in the threat model |
| **UX / Visual Designer** | Visual quality of generated UIs, docs and slides | Design tokens, house style, critique reports | Lighthouse a11y ≥ 95; vision-critique pass |
| **Technical Writer** | Docs in consultancy house style | README, RUNBOOK, quick-start, templates | Pandoc/Typst builds clean; judge rubric ≥ 4/5 |
| **Data / Computation Analyst** | Numeric correctness | Computation eval set, notebooks | 100% on deterministic math/data checks |
| **DevOps / SRE** | launchd, Colima, compose, observability, backups, reclaim | Service definitions, `law doctor`, soak results | 2-hour soak with no crash; RAM stays under budget |
| **Independent Reviewer / Judge** | Gives the pass/fail verdict on every deliverable. Uses a different model family from the generator (Gemma 4 locally; a Claude subagent during the build) | Scored rubric (JSON) with reasons | Does not edit what it judges. Verdicts are stored in `~/LAW/outputs/test-artifacts/verdicts/` |

**Generator → judge loop** (used by the local agents too):
1. Draft.
2. Self-check against the role checklist.
3. Run the deterministic checks.
4. The judge scores against the rubric.
5. If the score is below threshold, revise. Allow at most 3 loops, then escalate to the user with the evidence.

---

## §3 Phases

Every phase has: **Entry**, **Tasks**, **Exit gate** (evidence) and **Sign-off** (Solution Architect + Reviewer). Log every task.

### P0 — Bootstrap
- **Entry:** you are in `v6/`; `PROGRESS.md` exists.
- **Tasks:**
  1. `git init -b v6`; add `.gitignore` (excludes `.env*`, secrets and anything machine-local), `.editorconfig`, the pre-commit config (gitleaks first) and `justfile`. Install Git LFS and track `trained/**` binaries. Ask the user for the git remote URL and push; recommend a **private** repo, because `knowledge/` and voice clips are personal.
  2. Preflight script `scripts/preflight.sh`: checks macOS version, arm64, RAM, free disk (≥ 250 GB, or warn), Xcode CLT, Homebrew, network.
  3. Baseline disk report: run `du -sh` on every inventory path, plus `df -h /`. Save it to `~/LAW/logs/baseline-<ts>.txt`. Create `~/LAW` and its skeleton only.
  4. Create `.claude/agents/*.md` from §2.
- **Exit:** preflight passes; baseline is saved; first commit made.

### P1 — Research verification & architecture
- **Tasks:**
  1. For every row of `RESEARCH.md` §5 marked **(verify)**, plus every chosen component, confirm:
     - latest stable version;
     - license;
     - last release ≤ 90 days ago;
     - Apple Silicon support;
     - known issues.
     Use web search and fetch. Update RESEARCH.md with a dated "Verified" column.
  2. Run the model bake-off plan. **Shortlist only here; benchmarks happen in P4.**
  3. Write the ADRs:
     - 0001 runtime;
     - 0002 model roster;
     - 0003 hub/UI;
     - 0004 media stack;
     - 0005 data layout;
     - 0006 security posture;
     - 0007 eval/judge design;
     - 0008 installer design.
  4. Write `docs/ARCHITECTURE.md`: C4 context and container diagrams (Mermaid), port map, data flows, memory budget, NFRs, threat model (STRIDE).
- **Exit:** ADRs are accepted by the Solution Architect and the judge, and the user has seen a 10-line summary.

### P2 — Inventory & wipe (DESTRUCTIVE: requires explicit user "yes")
- **Tasks:**
  1. Write `scripts/wipe_legacy.sh`.
     - It is **dry-run by default** and runs for real only with `--apply`.
     - It uses a data-driven list `settings/legacy_inventory.yaml`, which you build from the PLAN.md inventory **plus a fresh scan**:
       ```bash
       ls -la ~ ~/.config ~/.cache ~/.local/share ~/.local/bin ~/Library/LaunchAgents
       find ~ -maxdepth 3 \( -iname '*law*' -o -iname '*mlx*' -o -iname '*codex*' -o -iname '*colima*' -o -iname '*lima*' -o -iname '*ollama*' -o -iname '*openclaw*' -o -iname '*aiws*' -o -iname '*ace-step*' \) -not -path '*/Library/Containers/*'
       ```
     - Each entry has: path, reason, size, and a category (`delete` | `archive-then-delete` | `ask` | `keep`).
  2. **Archive first** to `~/LAW/archive/legacy-<date>/`:
     - `~/.law/.env`, `~/.law/config/`, `~/.local-ai-workstation/.env`;
     - v4 `personas_enhanced.json` and `enhancement/` results;
     - v5 `registry.yaml`;
     - a full `tar --zstd` of the `v4/` and `v5/` folders.
     Redact secrets in a separate copy; keep the originals 0600.
  3. **Stop and unload:**
     - `launchctl bootout gui/$(id -u)/<label>` for every legacy LaunchAgent: `com.law.*`, `com.mlxstack.*`, `com.aiws.*`, `ai.openclaw.*`;
     - kill any leftover `mlx*`, `litellm`, `acestep`, `socat` processes;
     - `colima stop`, then `colima delete -f` (all profiles).
  4. **Delete** the inventory items:
     - `~/.law`, `~/.local-ai-workstation`, `~/.mlx-ai-workstation` (if present), `~/.colima`, `~/.lima` (if present), `~/ace-step`, `~/MLX-AI`, `~/.codex`, `~/.local-agent`;
     - the **entire** `~/.cache/huggingface`;
     - `~/.cache/uv`, the uv tools (`litellm`, `mflux`) and uv-managed Pythons in `~/.local/share/uv`;
     - `~/Library/Caches/ms-playwright`;
     - `~/.config/opencode/*` (the v6 config is regenerated later);
     - the `~/.local/bin` legacy shims: `law`, `lite*`, `litellm*`, `mflux-*`, the broken `nano-pdf`. **Keep `claude`.**
     - `$TMPDIR/mlx-openai-prompt-cache-*`, `$TMPDIR/pytest-of-$USER`;
     - every v4/v5 SQLite database and queue file (`queue.db`, `agent.db`, `rag.db` and any `*.db`/`*.sqlite` under the legacy roots), and any Python SQLite-queue packages in the legacy venvs. v6 does not use SQLite for its queue.
     - Homebrew `sqlite`: remove only if `brew uses --installed sqlite` shows nothing depends on it (Python and many tools do); otherwise keep it and record why. Ask first.
     - `.local-agent/` folders inside `~/SourceCode/*` projects. **List them and ask first.**
  5. **Shell profile.** Back up `~/.zprofile`, then remove only lines that reference law, mlx, `~/.local/bin` (if added by v5), or OLLAMA. Show the diff and ask.
  6. **APFS local snapshots** (`tmutil listlocalsnapshots /`): list them and ask before deleting.
  7. **Brew.** List the candidates: colima, lima, docker, docker-compose, socat, codexbar, openai-whisper, pytorch, and any others recorded in `owned.json`. Ask per package. v6 reinstalls the ones it needs from its own Brewfile.
     - Also ask about the VS Code extension `openai.codex-audio`, and about `~/OneDrive/AI-Agent` if it exists.
  8. **Delete the `v4/` and `v5/` folders** after confirming the archive integrity (`tar -tf` succeeds and sizes match).
  9. **Re-scan.** Write `~/LAW/logs/wipe-report-<ts>.md` with a before/after `du` and `df` table, and record the total GB reclaimed in PROGRESS.md.
- **Tests:** a bats or pytest suite runs `wipe_legacy.sh` against a **fake HOME** seeded with every inventory item. It asserts that dry-run deletes nothing, that apply deletes exactly the `delete` set, that `keep` items survive, and that a second run is a no-op.
- **Exit:** the wipe report shows zero remaining legacy items. The user confirms.


### P3 — Installer (`install.sh` + `law` CLI)
- **Design (Solution Architect owns ADR-0008):**
  - `install.sh` (≤ 150 lines, shellcheck-clean): preflight → Xcode CLT → Homebrew → `brew bundle --file=Brewfile` → `uv` → create `~/LAW` skeleton → write `~/LAW/state/env.sh` → add one marked `source` line to `~/.zprofile` → `uv sync` the control venv → `exec law install`.
  - `law`: Python 3.12, Typer + Rich, pydantic models, uv-locked (`pyproject.toml`, `uv.lock`). Package layout:
    ```
    v6/
      install.sh  Brewfile  compose.yaml  justfile  pyproject.toml  uv.lock
      settings/  knowledge/  trained/   # portable state (see §1 rule 1)
      src/law/{cli,registry,plan,apply,steps/,services/,models,doctor,reclaim,backup,uninstall,sysapi}.py
      templates/{launchd,docs,projects}/
      evals/{promptfoo,inspect,golden}/
      tests/{unit,integration,e2e}/
      docs/{ARCHITECTURE.md,RUNBOOK.md,QUICKSTART.md,TEST-REPORT.md,adr/}
      .claude/agents/  .github/workflows/ci.yml  .pre-commit-config.yaml
    ```
  - Carry over from v5: plan → apply → verify reconcile with `--dry-run`; `owned.json` (only remove what we installed); 7-day `~/LAW/trash`; a `SysAPI` abstraction wrapping brew, launchctl, docker, hf, subprocess so every step is fakeable in tests; launchd plists embed the registry hash so changes trigger reloads.
  - Do **not** carry over: custom gateway, custom dashboard, the monolithic Telegram bot, the SQLite job queue, inline heredoc copies of code. (Telegram returns as a thin modular adapter in P4b.)
- **Fresh-Mac bootstrap.** Preconditions are only Homebrew and Python (git comes with the Xcode CLT that Homebrew installs). `install.sh` installs everything else: Brewfile, uv, Colima, Docker CLI, Node, git-lfs (`git lfs pull`), venvs, models, containers, launchd services. It must not assume anything from a previous install.
- **Secrets on a fresh machine:** prompt once interactively, or read env vars (`LAW_TELEGRAM_TOKEN`, `LAW_TELEGRAM_USER_IDS`, `HF_TOKEN`), or import a user-kept file (`./install.sh --secrets <file>`). `law secrets export` writes that file for the user's password manager. Secrets are never written into v6.
- **Portable state:**
  - `law restore-state` (also runs inside every `law install`) loads `v6/settings`, `v6/knowledge` and `v6/trained` into the services (Open WebUI import, knowledge collections, ComfyUI workflows, LoRAs, voices) and rebuilds the derived indexes in `~/LAW/data`.
  - `law save` exports what is worth keeping from the live services (Open WebUI presets/prompts/tools/knowledge definitions, agent memory, new knowledge docs, role and Telegram settings without tokens) back into v6, runs the secret scan, and commits only if it is clean. Optional nightly launchd run; Telegram `/save` triggers it too.
- **Re-run guarantees (each one has a test in §4):**
  1. Fresh run → healthy.
  2. Immediate re-run → 0 changes.
  3. Re-run after an interruption at any step (killed mid-step) → converges.
  4. Re-run after manual damage (deleted venv, plist, container or model) → repairs only that piece.
  5. Re-run after `git pull` with changed settings → applies only the diff.
  6. Re-run after `rm -rf ~/LAW` → full rebuild from v6, including settings and knowledge.
  Every step is check → plan → apply → verify, takes a lock, and backs up to `~/LAW/trash` before anything destructive.
- **Admin commands:** `law install | status | doctor | update | models {list,pull,rm,verify} | services {start,stop,restart,logs} | reclaim | save | restore-state | secrets {set,export} | uninstall [--system]`. AI commands are defined in P4b.
- **Services (all 0.0.0.0, no login):** oMLX (LLM API), `law` API, Open WebUI, SearXNG, Homepage, ComfyUI, Valkey (queue broker), optional Langfuse. Port map lives only in `registry.yaml`. `law doctor` prints the LAN URLs for other devices.
- **Secrets:** generated on first run into `~/LAW/state/.env` (0600); Open WebUI admin created via env, `WEBUI_SECRET_KEY` set.
- **Exit:** `law install --dry-run` shows the full plan; real install succeeds; second run = 0 changes; `law doctor` green; unit tests ≥ 85% coverage.

### P4 — Runtime & models
- **Bake-off (ADR-0001, ADR-0002):** oMLX vs `mlx_lm.server`/`mlx_vlm.server` behind llama-swap. Measure on the same models: tool-call success on 50 calls, vision Q&A, OpenAI + Anthropic API compatibility (Claude Code and OpenCode both work), cold start, tokens/s, peak RAM, and a 2-hour soak with no crash. Pick the winner by score, not preference.
- **Models:** pull the roster from `registry.yaml` into `HF_HOME=~/LAW/models/hf`, pinned by revision, verified by size/hash. Default roster (re-verify in P1):
  - Qwen3.8-27B 8-bit (reasoner/writer/vision), Qwen3-Coder-Next 4-bit (coder), Qwen3.6-35B-A3B (fast), Gemma 4 31B 4-bit (judge), Qwen3-Embedding + Qwen3-Reranker.
- **Memory safety:** one large model resident; context caps per model; `kv_bits 8`; RAM guard unloads the LLM before media jobs; never raise `iogpu.wired_limit_mb` without asking.
- **Exit:** bake-off report in `docs/adr/`; every model passes a load + generate probe; soak passes.

### P4b — Job queue, model scheduler, agents, Telegram & CLI (the core of v6)
**Goal:** many requests from many devices are handled correctly, in order of priority, using the full 64 GB and M5 Pro, with the best model for each job swapped in as needed. Quality over speed: waiting is fine, bad output is not.

- **Queue (ADR-0009; re-verify in P1):** Valkey (BSD-3, Redis-compatible) with AOF persistence in `~/LAW/docker/valkey`, plus a maintained Python job framework (candidates: Dramatiq, Taskiq, arq, Celery; pick by maintenance, priorities, retries, result storage). No SQLite queue.
  - Features: priorities (interactive Telegram/CLI > Open WebUI agent jobs > background batch); retries with backoff; idempotent job IDs; cancel; pause/resume; survives worker crash and reboot; dead-letter queue; per-job progress events; full job history queryable by `law jobs`.
- **Model-aware scheduler (custom glue, kept small and fully tested):**
  - Every job declares its resource needs from `registry.yaml`: model ID, estimated memory, lane (`gpu-heavy` for LLM-large/image/video/music, `light` for embeddings/STT/TTS/small models).
  - Memory admission: model budget = 64 GB − 12 GB macOS reserve − Docker allocation. Light jobs run concurrently while they fit; one heavy model at a time.
  - Swap minimisation: group queued jobs by model (affinity batching) with aging so nothing starves; prefetch the next model's weights into page cache while the current job finishes when memory allows; unload through the runtime's API, never by killing processes.
  - Concurrent requests to the **same** loaded LLM use the runtime's continuous batching.
  - Interactive chat from Open WebUI goes straight to the LLM runtime; the scheduler coordinates by unloading/reloading through the runtime API before heavy media jobs. Verify the runtime's model-management API in P1.
- **Multi-model pipelines for quality:** e.g. coder drafts → judge (different family) reviews → coder revises; writer drafts → judge scores; image generated → vision model checks prompt adherence → regenerate if it fails. The scheduler swaps models between stages. Use the largest quant that fits per role.
- **Agents:** runtime agents use an existing engine. Default: OpenCode in server mode (`opencode serve`) with role agents generated from `roles.yaml`, permissions all set to allow; ADR may pick Goose instead after evaluation. Code projects they create go to `~/SourceCode/<project>/`. Agents have shell, file, network, browser (Playwright), web search, RAG, media tools and access to the whole machine.
- **Computer use (full Mac control, user requirement):** agents can operate the Mac's apps and GUI, not just the shell. Example: install Spotify, open it, search for a song, play it, set the volume. Build it from existing tools (verify the best maintained options in P1, record the choice in ADR-0010):
  - **Scriptable control first** (most reliable): `open -a`, AppleScript/JXA via `osascript` (app scripting dictionaries, System Events), the `shortcuts` CLI, `brew install --cask` for apps, `defaults`, `pmset`, volume/brightness/media keys.
  - **GUI control when an app has no scripting interface:** a macOS accessibility-tree automation tool (e.g. Peekaboo or a macOS-automation MCP server) for screenshots, element lookup, click, type and scroll; `cliclick` as a low-level fallback.
  - **Browser:** Playwright with a persistent profile under `~/LAW/data/browser`, for web tasks and downloads.
  - **Seeing the screen:** the vision-language model (Qwen3.8-27B VL) reads screenshots to plan the next action and to verify each step worked ("is the song playing?").
  - All of these are exposed to the agents as MCP tools and to Telegram/CLI as normal tasks (e.g. `law agent operator "play some jazz on Spotify at 40% volume"`). Add an **Operator** role to `settings/roles.yaml` for these tasks.
  - **macOS permissions (TCC):** Accessibility, Screen Recording, Automation (Apple Events), Full Disk Access and Input Monitoring can't be granted by a script. `install.sh` opens each System Settings pane in turn with clear instructions for which app to tick (Terminal/the `law` worker/the automation tool). `law doctor` checks each grant and says which is missing. This is a manual step after every fresh install.
- **`law` API (`law serve`, 0.0.0.0, no auth):** a small FastAPI service that accepts jobs, streams progress (SSE/WebSocket), and returns results/files. Telegram, the CLI and the Homepage dashboard all use it.
- **CLI (`law`), as in v5 but cleaner:**
  `law ask "<q>"` · `law agent <role> "<task>"` (roles from `roles.yaml`, e.g. architect, coder, writer, designer, analyst, reviewer) · `law code "<task>" [--project DIR]` · `law doc "<brief>" [--format docx|pdf|pptx]` · `law image "<prompt>"` · `law edit <img> "<instruction>"` · `law video "<prompt>" [--image IMG]` · `law tts "<text>"` · `law stt <audio>` · `law music "<prompt>"` · `law search "<q>"` · `law rag {add,ask}` · `law jobs [--watch]` · `law cancel|retry|resume <id>`. Works on this Mac and from other devices (`LAW_SERVER=http://<mac>.local:<port>`).
- **Telegram (primary channel), thin and modular:** a maintained library (aiogram or python-telegram-bot; verify), long polling (no inbound port), split into small modules (commands, media, jobs, formatting). Each command maps one-to-one to a `law` CLI command. Voice notes → STT → task; photos → vision or edit; documents → RAG or edit; results returned as messages/files; live progress by editing a status message; inline buttons for cancel/retry; long outputs sent as files. Allow-list by Telegram user ID.
- **Exit:** the queue tests in §4 pass; the same task gives the same result from Telegram, CLI and API.

### P5 — Capabilities (wire existing tools; no custom UIs)
For each capability: BA writes acceptance criteria (Gherkin) → Developer wires it → QA adds an eval → Judge verifies.
| Capability | Wiring |
|---|---|
| Chat, reasoning, judgement | Open WebUI model presets generated from `roles.yaml` |
| Coding | OpenCode config + Claude Code on the local Anthropic endpoint (`ANTHROPIC_BASE_URL`), plus `copier` project templates (see P6) |
| Research | SearXNG as Open WebUI web-search provider; RAG with Qwen3 embedding + reranker; PDF/Office ingestion |
| Computation | Open WebUI code interpreter (Jupyter), JupyterLab, scientific Python stack in `~/LAW/runtime/media` |
| Documentation | pandoc, Typst, Marp, Mermaid, LibreOffice headless; corporate house-style templates in `templates/docs/` (memo, report, proposal, slide deck) |
| Design | Playwright screenshots + vision-model critique against a design checklist; design tokens in templates |
| Image gen/edit | ComfyUI (models in `~/LAW/models/comfy`, saved workflows in `templates/comfy/`) as Open WebUI image backend; mflux CLI for FLUX.2, Qwen-Image, Qwen-Image-Edit, upscaling |
| Video gen/edit | LTX-2.x (MLX) and Wan 2.2; ffmpeg, auto-editor, PySceneDetect via `law media` thin wrappers |
| Audio | mlx-audio (Kokoro TTS, Whisper STT) wired into Open WebUI voice; Chatterbox (voice cloning of the user's own voice); ACE-Step 1.5 (music); Demucs (stems) |
| Computer use | Operator role + automation MCP tools (osascript/Shortcuts, accessibility-tree GUI tool, Playwright, vision model for screenshots); TCC permission checks in `law doctor` |
| Ops | Homepage dashboard listing every service with health widgets; optional Langfuse traces |
- **Exit:** every capability has AC + a passing smoke test.

### P6 — Quality system (makes local-agent output production-ready)
- `settings/roles.yaml` → role prompts with checklists (bank/consultancy standard: executive summary first, sourced claims, consistent formatting, no placeholders).
- **Generator → judge loop** script `law review <path>`: runs deterministic checks for the artifact type, then the judge model scores against `evals/rubrics/<type>.yaml`; revise up to 3 times; store verdict JSON.
- **Project templates** (`copier`): Python service, TypeScript web app, data notebook, document pack. Each ships with pre-commit (ruff/mypy or biome/tsc, gitleaks, semgrep), tests with coverage gate, CI workflow, ADR folder, threat-model section, README, CHANGELOG, LICENSE.
- **Exit:** a sample project generated by the local coder passes its own CI and the judge rubric ≥ 4/5.

### P7 — Testing → see §4.

### P8 — Docs, handover, storage cleanup
- Write README, `docs/QUICKSTART.md` (one page), `docs/RUNBOOK.md` (start/stop, update, add a model, backup/restore, reset, troubleshooting), finalize ARCHITECTURE and TEST-REPORT.
- `law reclaim`: `brew cleanup -s`, `uv cache prune`, `docker system prune` (v6 resources only), HF cache prune of unregistered revisions, trash older than 7 days, old logs; print before/after.
- Final disk report vs the P0 baseline into PROGRESS.md.
- **Exit:** user walks through QUICKSTART successfully.

---

## §4 Testing strategy (SDLC-grade)

| Layer | Tooling | What it proves | Gate |
|---|---|---|---|
| Static | ruff, mypy --strict, shellcheck, hadolint, yamllint, gitleaks, semgrep, osv-scanner | Code quality and secrets/dependency hygiene | 0 errors |
| Unit | pytest + coverage, fake `SysAPI`, sandboxed `HOME` (tmp), launchctl/docker/brew blocked | Installer logic, registry validation, plan diffs, wipe logic | ≥ 85% line, ≥ 75% branch |
| Integration | pytest `-m integration` on the real Mac | Services healthy, every port reachable on the LAN IP with no login, idempotent re-install (0 changes), uninstall → reinstall round trip, backup → restore | all pass |
| E2E UI | Playwright | Open WebUI (no login): chat, RAG upload + cited answer, web search, tool call, image request, voice round trip; ComfyUI: run saved workflow; Homepage: all tiles green | all pass, screenshots saved |
| Capability evals | promptfoo + Inspect AI, golden sets in `evals/golden/` | See table below | thresholds met |
| Queue & scheduler | pytest + real Valkey | 30 mixed jobs submitted at once from Telegram (fake Bot API), CLI and API: all complete correctly; no OOM; priorities honoured; model swaps ≤ affinity-optimal + 2; `kill -9` worker mid-job → job resumes; reboot → queue intact; cancel works | all pass |
| Telegram | fake Bot API in tests + one real-bot smoke test | Every command, voice note, photo, document, progress updates, allow-list rejects other user IDs | all pass |
| CLI | pytest + subprocess | Every `law` AI command, locally and with `LAW_SERVER` pointing at the LAN IP | all pass |
| Re-run matrix | pytest `-m rerun` on the real Mac | The six re-run guarantees in P3, including interrupting each install step and `rm -rf ~/LAW` → full rebuild with settings and knowledge restored | all pass |
| Fresh machine | macOS VM (tart or UTM; verify free personal-use terms in P1) with only Homebrew + Python | `git clone` → `./install.sh --profile small` (tiny models for VM RAM) → `law doctor` green → CLI/API/Telegram (fake Bot API) smoke tests → second run = 0 changes | all pass |
| Secret leak | pytest + gitleaks | Seed fake tokens into the live services and `.env`, run `law save`, assert nothing secret reaches v6 and the commit is refused when a secret is planted in `knowledge/` | all pass |
| Non-functional | custom pytest + `psutil` | 2-hour soak, peak RAM < budget, cold-start times recorded, no zombie processes | no crash; RAM in budget |
| Regression | `just eval` re-run on model or config change; results diffed vs last baseline | No quality regression > 5% | pass |

**Capability eval thresholds** (deterministic checks first, then judge):
| Domain | Check |
|---|---|
| Coding | Local coder builds a small app from a spec via the full SDLC; its tests, lint, type check and scanners pass; judge ≥ 4/5 |
| Reasoning/judgement | Golden Q&A with known answers ≥ 85%; judge rubric for argument quality |
| Computation | 100% on numeric tasks verified by SymPy/NumPy |
| Documentation | pandoc/Typst build clean; judge rubric (structure, clarity, sourcing, formatting) ≥ 4/5 |
| Design | Generated page: Lighthouse a11y ≥ 95, vision critique passes checklist |
| Image gen | File valid, resolution correct; vision model confirms prompt elements present |
| Image edit | Before/after differ in the edited region only (SSIM outside mask ≥ 0.9) |
| Video | Frame count, duration, codec, audio track verified with ffprobe |
| TTS → STT | Round-trip WER ≤ 5% |
| Music | Duration within ±10%; loudness within range (ffmpeg ebur128) |
| RAG | Answer cites the correct source doc for 10/10 golden questions |
| Computer use | Scripted scenarios on the real Mac, each verified by deterministic state checks plus a screenshot judged by the vision model: install an app via cask, open it and quit it; open TextEdit, type and save a file (file content checked); set volume to 30% (`output volume of (get volume settings)` = 30); play then pause audio in Music/Spotify (player state checked); open a URL in the browser and download a file (file exists, size > 0). ≥ 9/10 pass on three runs |

**Commands:** `just lint`, `just test` (static + unit, < 2 min), `just integration`, `just e2e`, `just eval`, `just all`. CI (`.github/workflows/ci.yml`, macOS runner) runs static + unit. `docs/TEST-REPORT.md` is generated from raw results, never hand-written.

---

## §5 Resume protocol
1. Session start: read `CLAUDE.md` → `PROGRESS.md` → `progress.json`.
2. Run `law status` (if installed) and `git log --oneline -5` to confirm the actual state matches the log.
3. Continue at the first unchecked task. If state and log disagree, trust the actual state, fix the log, then continue.
4. Before any long step, write `next_step` so a fresh session can pick it up.

## §6 Definition of done
- All §0 success criteria met with evidence linked in PROGRESS.md.
- All phase gates signed off (Solution Architect + Judge).
- `just all` green three runs in a row.
- Wipe report and final storage report recorded.
- User acceptance: QUICKSTART walkthrough completed.

---

## §7 Traceability to `idea.txt`
| idea.txt requirement | Where covered |
|---|---|
| Study v4 and v5 | PLAN.md (findings); P1 |
| Local, free/open-source, powerful on M5 Pro 64 GB, accuracy over speed | §0, RESEARCH.md §5–6, P4 |
| Complete plan | PLAN.md, this brief |
| Coding, design, documentation, judgement, computation, photo/video/audio | P5 table, §4 evals |
| (Pen testing) | Removed from scope at the user's request (2026-10-19) |
| v6 setup script with much better setup & configuration | P3 |
| Wipe v4/v5, fresh install via scripts | P2, P3 |
| Detailed prompt for Claude Code | This file + CLAUDE.md |
| End-to-end testing | §4 |
| Reuse existing tools/dashboards | §1 rule 6, P5 (Open WebUI, ComfyUI, Homepage) |
| All free, no subscriptions | RESEARCH.md §8 |
| Detailed internet research, best current solutions | RESEARCH.md; P1 re-verification |
| Progress logging and resume | §1 rules 2–3, §5, PROGRESS.md |
| Storage cleanup | P2 wipe report, P8 `law reclaim` |
| Code in ~/SourceCode (released code in v6); everything else in ~/LAW | §1 rule 1 |
| Settings/knowledge/trained data in v6, no secrets; clone on a new Mac = same setup | §1 rule 1, P3 portable state, §4 fresh machine + secret leak |
| Script safe to run many times | P3 re-run guarantees, §4 re-run matrix |
| Agents with real full access: open/use apps, browse, download, play music, control volume | §1 rule 7, P4b computer use, §4 computer-use eval |
| Telegram as main channel; `law` CLI triggers agents | P4b |
| Open on LAN (0.0.0.0), no login, agents with full machine access | §1 rule 7, P3, P4b |
| Remove v5 SQLite queue; robust queue using full 64 GB / M5 Pro; swap powerful models | P2, P4b |
| Robust E2E, SDLC, production-ready, bank/consultancy grade | §2, P6, §4 |
| BCG-style research; best tech stack; Solution Architect role | RESEARCH.md, P1, §2 |
