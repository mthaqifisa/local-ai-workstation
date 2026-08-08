# LAW — Local AI Workstation

> 100% local AI stack on Apple Silicon. No API keys, no billing, no data leaving your machine.

A production-grade local AI setup for Apple M-series Macs. One command brings up a multi-agent orchestrator backed by 9 models, a full web dashboard (**LAW**), a Telegram bot, image generation, vision analysis, text-to-speech, video generation, and full autonomous SDLC capability.

---

## What's New — v4.2 Phase 6 Media Module (2026-08-08)

### Text-to-Speech Studio
Full Kokoro-82M TTS integrated into the dashboard at `/audio`:
- **6 voices** — af_sky, af_heart, am_adam, bm_lewis, bf_emma, am_echo
- **Speed control** — 0.5× to 2.0× with live label
- **Format** — WAV or MP3 output
- **Library tab** — scrollable gallery of all previous generations with inline playback and delete
- Single-file output via `--join_audio` — no fragmented `_000.wav` chunks

### Video Generation Studio
LTX-2.3 local video generation at `/video` (no Replicate, no API key):
- **Text-to-video** — describe a scene, get an MP4 in ~5 minutes on M5 Pro
- **Image-to-video** — upload a source frame, animate it
- **Text encoder choice** — Gemma-3-12B or Gemma-4-12B for different prompt styles
- **Async job polling** — fire and check; dashboard polls job status every 3 s with a progress bar
- **Gallery** — thumbnail previews, inline player, delete button

### Image Editing (img2img)
New edit mode on the Create page alongside text-to-image:
- Upload any image as a base, describe your changes
- Powered by Qwen FLUX in image-to-image mode (`--image-strength 0.65`)
- Edit tab sits next to the Generate tab — same UI, same gallery

### Vision Revamp
`/vision` page upgraded:
- **Drag-and-drop** upload zone replaces the file picker
- **Model dropdown** — switch between Qwen3-VL-8B and Gemma-4-12B without leaving the page
- Image preview before submission with clear button

### Chat Enhancements
- **Voice mode** — one-click TTS readback of agent responses (af_sky voice, auto-plays)
- **Inline image generation** — type `/imagine <prompt>` in chat to generate and display an image inline
- **Session persistence** — conversation history survives page refresh via `sessionStorage`
- **Response delete** — remove individual messages from the thread

---

## What's New — v4.1-elite (2026-08-08)

### 16 Elite Personas
Expanded from 11 to 16 fully-trained personas. All upgraded to Fable-5 / Devin / Kimi-K3 benchmark quality with hardened system prompts, tool discipline, and anti-hallucination tuning:

| New Personas | Role |
|---|---|
| `security` | STRIDE threat modelling, OWASP Top 10, active pentest guidance |
| `data` | EDA → model → evaluate → interpret full pipeline |
| `devops` | IaC, GitOps, hardening, CI/CD pipelines |
| `architect` | C4 diagrams, ADRs, CAP theorem, 12-factor app design |
| `imagegen` | Dedicated FLUX image specialist — prompt engineering, resolution, style presets |

### Image Generation — First-Class Tool
Agents can now generate images directly via the `generate_image` tool:

```bash
mlx-agent "Generate a photorealistic image of a mountain at sunrise, 1024x1024"
# → agent calls generate_image tool → FLUX runs → saves PNG → reports path + size
```

### Dashboard Create Page — Midjourney-Quality UI
Redesigned `/imagine`: model chips · 8 style presets · resolution grid · steps slider (4–16) · seed controls · negative prompt · live progress · gallery with lightbox.

### Stability Fixes
- `MAX_TOOL_CALLS_PER_ROUND = 6` — prevents kernel panics from runaway loops
- Context window 65 536 → 32 768, prompt cache 8 GB → 4 GB for large models
- Port 8000 health check — agent restarts inference service if dead
- deepseek-r1-70b → 32b automatic fallback on server error

---

## Architecture

![Architecture Diagram](screenshots/architecture.png)

Full stack: user interfaces → LiteLLM role-alias gateway → mlx-openai-server on Apple Metal → 9 quantized models in HuggingFace cache. Sequential multi-agent SDLC loads one model at a time, allowing 42 GB+ models to run on 64 GB.

---

## Screenshots

### Main Dashboard
![Main Dashboard](screenshots/dashboard-main.png)

Three-column layout: **System Core** (service health, model roster) · **Neural Activity** (live SSE event feed + active agent tracker) · **Command** (quick dispatch + 6-metric sparklines: CPU, Memory with GB, Battery, Storage with GB, Disk I/O, Network I/O).

### Chat
![Chat](screenshots/dashboard-chat.png)

Full-screen chat in **Agent** mode (tool-calling with thinking panel + step trace) or **Direct** mode (raw model, faster). Supports image attachments for vision-capable models.

### Vision Analysis
![Vision Page](screenshots/dashboard-vision.png)

Drop an image, pick a vision model from the dropdown, ask a question. Runs Qwen3-VL-8B or Gemma-4 locally. No data leaves the machine.

### Image Generation (Create)
![Create Page](screenshots/dashboard-create.png)

Redesigned Midjourney-style UI: **model chips** (Z-Image Turbo / FLUX schnell / FLUX dev) · **style presets** (8 options) · **resolution grid** with visual icons · **steps slider** (4–16) · **seed controls** · **negative prompt panel** · **live progress bar** · **gallery** with hover-overlay download + lightbox. ~15 s for 1024×1024 on M5 Pro.

### Audio / TTS Studio
![Audio Page](screenshots/dashboard-audio.png)

Kokoro-82M TTS running entirely on-device. Pick a voice, set speed, generate — audio plays inline and saves to the Library. No Eleven Labs, no API key.

### Video Studio
![Video Page](screenshots/dashboard-video.png)

LTX-2.3 text-to-video and image-to-video. Describe a scene, pick a text encoder, click Generate — the dashboard polls the async job and shows the MP4 in the gallery when done. ~5 min on M5 Pro.

### Personas
![Personas](screenshots/dashboard-personas.png)

Create and edit agent personas — each is a name + model alias + system prompt + allowed tools.

### Models
![Models](screenshots/dashboard-models.png)

Search HuggingFace, download new models, probe-load to test, and register into the serving config — all from the browser.

### Mobile — Dashboard
![Mobile Dashboard](screenshots/dashboard-mobile.png)

On mobile the cards reorder: **Command** first (quick dispatch + metrics), then Neural Activity, then System Core. Hamburger nav in the top-right.

### Mobile — Nav Open
![Mobile Nav Open](screenshots/dashboard-mobile-nav.png)

Floating nav overlay — drops over content without pushing the layout, blurred backdrop to dismiss.

### Mobile — Sub-page Nav
![Sub-page Nav](screenshots/dashboard-subnav-mobile.png)

Every sub-page (Chat, Vision, Create, Personas, Models) has the same floating LAW nav, fixed to the top of the viewport.

### Mobile — Chat
![Chat Mobile](screenshots/dashboard-chat-mobile.png)

Chat on mobile — compact header (mode toggle + model select only), internal scroll thread, full-height viewport with iOS `100dvh` fix.

### Mobile — Personas List
![Personas Mobile](screenshots/dashboard-personas-mobile.png)

Personas list on mobile — single-column card grid.

### Mobile — Personas Dialog
![Personas Dialog Mobile](screenshots/dashboard-personas-dialog-mobile.png)

Personas edit dialog on mobile — internally scrollable with sticky Save/Cancel footer, single-column layout, `font-size:16px` inputs to prevent iOS auto-zoom.

---

## Hardware target

| Component | Spec |
|---|---|
| Chip | Apple M5 Pro (works on M1–M5) |
| RAM | 64 GB unified memory |
| Disk | 1 TB |
| OS | macOS 13+ |

All model sizes listed are peak MLX memory. The **on-demand loader** keeps only one model in RAM at a time, so 64 GB is enough to run the 42 GB coder or 37 GB deep reasoner — just not simultaneously.

---

## Architecture

```
You
 │
 ├── Web Dashboard  :8800  — chat, vision, image gen, live metrics, personas, models
 ├── Telegram Bot          — mobile-first, live thought bubbles, approval buttons
 └── CLI: mlx-agent        — terminal REPL, batch tasks, autonomous SDLC

         ↓ OpenAI-compatible API calls

   LiteLLM Gateway  :4000  — role aliases (coder:, writer:, reasoner:…)
         ↓
   mlx-openai-server  :8000  — on-demand model loader, Apple Metal GPU
         ↓
   9 local models in HuggingFace cache (~280 GB total, all quantized)
```

### Sequential multi-agent SDLC

The orchestrator (Qwen3.6-35B MoE) **plans then delegates one subagent at a time**. Each specialist loads, runs its task, then unloads before the next one loads. This allows models that individually exceed available RAM.

```
User: "build a wordcount CLI: code + tests + README"
  → Orchestrator plans  (Qwen3.6-35B,  ~20 GB loaded)
  → unloads
  → Coder writes code   (Qwen3-Coder-Next 80B, ~42 GB)
  → unloads
  → QA writes tests     (Qwen3.6-27B,  ~28 GB)
  → unloads
  → Writer writes docs  (Qwen3.6-27B)
  → Orchestrator reports back
```

Every agent step streams to the dashboard's **Neural Activity** feed via SSE (`activity/events.jsonl`). You watch the pipeline execute in real time.

---

## Model roster

| Role | Model | Size | Notes |
|---|---|---|---|
| Orchestrator / Researcher / BA | unsloth/Qwen3.6-35B-A3B MoE | ~20 GB | 3.3B active params, tool calls, CoT |
| Writer / QA | mlx-community/Qwen3.6-27B 8-bit | ~28 GB | Dense, stable long output |
| Fast Reasoner | mlx-community/DeepSeek-R1-Distill-Qwen-32B | ~18 GB | `<think>` CoT, MIT license |
| Deep Reasoner | mlx-community/DeepSeek-R1-Distill-Llama-70B | ~37 GB | Best for hard math / planning |
| Best Coder | mlx-community/Qwen3-Coder-Next (80B MoE) | ~42 GB | SWE-Bench #1, tool calls |
| RAG Embeddings | mlx-community/Qwen3-Embedding-8B | ~5 GB | Vector search |
| Vision / OCR | mlx-community/Qwen3-VL-8B | ~5.4 GB | SOTA small VLM |
| Design / Vision | mlx-community/Gemma-4-12B | ~10 GB | Google Gemma 4 |
| General Purpose | mlx-community/Gemma-4-31B | ~18 GB | Google Gemma 4, larger |
| **Image Gen** | Tongyi-MAI/Z-Image-Turbo (mflux) | ~32.8 GB | Non-gated, ~15 s/512 px |

All LLMs served with `on_demand: true`, `kv_bits: 8` (halved KV-cache), per-model context windows.

---

## Anti-hallucination config

Precision-first parameters applied to every model call:

| Parameter | Value | Effect |
|---|---|---|
| `temperature` | 0.1 | Near-deterministic output |
| `frequency_penalty` | 0.6 | Suppresses repetition loops |
| `min_p` | 0.05 | Filters low-probability tokens |

Per-model temperature overrides are applied where needed (e.g. embeddings → 0.0).

---

## Services

| Service | Port | Purpose |
|---|---|---|
| mlx-openai-server | 8000 | Apple Metal inference, OpenAI API |
| LiteLLM Gateway | 4000 | Role-alias routing, drop-in OpenAI proxy |
| LAW Dashboard | 8800 | Full web UI |
| Open WebUI | 3001 | Alternative chat frontend |
| SearXNG | 8888 | Private web search for agent RAG |
| Langfuse | 3000 | Optional tracing / observability |
| Portainer CE | 9000 / 9443 | Docker image & container manager |

All services managed by **launchd** — start at login, auto-restart on failure.

> **Portainer + Colima:** Colima's Unix socket (`~/.colima/default/docker.sock`) is bridged to TCP 2375 via a `socat` launchd service (`com.mlxstack.docker-bridge`). Portainer connects via `host.docker.internal:2375` instead of a direct socket mount.

---

## Setup

### Prerequisites

- macOS 13+ on Apple Silicon (M1–M5)
- Xcode CLT: `xcode-select --install`
- Homebrew: `brew.sh`
- `uv`: `brew install uv`

### Bootstrap

```bash
git clone <this-repo>
cd local-ai-workstation
chmod +x mlx-setup.sh
./mlx-setup.sh --bootstrap
```

This installs the Python venv, all dependencies, downloads core models, writes all configs, and registers launchd services. Everything auto-starts from this point on.

### Configure secrets

```bash
./mlx-setup.sh --configure
```

Prompts for `DASHBOARD_PASSWORD`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_USER_ID`. Stored in `~/.mlx-ai-workstation/.env` (never committed).

### Pull heavy models (optional, ~79 GB extra)

```bash
./mlx-setup.sh --pull-heavy   # Qwen3-Coder-Next 80B + DeepSeek-R1-70B
```

---

## Usage

### LAW Dashboard — `http://localhost:8800`

| Page | What it does |
|---|---|
| **Dashboard** | Live CPU/RAM/Battery/Disk/Net sparklines, service health, active agent tracker, SSE neural feed, quick dispatch |
| **Chat** | Full-screen chat — Agent mode (tool-calling + thinking panel) or Direct mode. Voice readback. Inline `/imagine`. Session persistence. |
| **Vision** | Drag-and-drop image → pick model (Qwen3-VL-8B or Gemma-4-12B) → local OCR / image Q&A |
| **Create** | Text-to-image + img2img editing. Model chips (Z-Turbo / schnell / dev), 8 style presets, resolution grid, steps slider, seed, negative prompt, gallery |
| **Audio** | Kokoro-82M TTS — 6 voices, speed control, WAV/MP3, Library gallery with inline playback |
| **Video** | LTX-2.3 text-to-video and image-to-video — async job polling, Gemma text encoder, gallery with player |
| **Personas** | Create / edit agent personas (model + system prompt + tool permissions) |
| **Models** | Search HuggingFace, download, probe-load, register new models |

The top navigation is **sticky on all pages** — always one click away from any section.

### CLI agent

```bash
# Ask orchestrator a question
mlx-agent "how much disk is free on this laptop?"

# Use a specific specialist (16 personas total)
mlx-agent --persona coder      "write a binary search tree in Python"
mlx-agent --persona researcher "what are the best MLX models in 2026?"
mlx-agent --persona imagegen   "generate a cinematic portrait of a cyberpunk city, 768x1280"
mlx-agent --persona security   "STRIDE threat model for this REST API design"
mlx-agent --persona architect  "design the data layer for a multi-tenant SaaS, C4 context diagram"
mlx-agent --persona data       "EDA and feature importance for this CSV"
mlx-agent --persona devops     "write a Kubernetes deployment manifest with resource limits"

# Interactive REPL
mlx-agent

# Full autonomous SDLC task
mlx-agent --persona orchestrator --yes "
  Build a Python CLI for parsing nginx access logs.
  Have coder write it, QA write tests, writer write README.
  Save everything to ~/Projects/nginx-parser/.
"
```

### Telegram bot

After `./mlx-setup.sh --configure`, the bot starts automatically at login.

```
/help          — show commands
/personas      — list available agents
/use coder     — switch to coder persona
/reset         — clear conversation history

Any other text → dispatched to the current persona

While working:
  🧠 orchestrator working… ⠙ 12s     ← live spinner
  ▸ tool: run_shell                    ← tool call trace
  Loading model… (45s)                 ← cold-start indicator
  [Yes] [No] [Allow all]               ← approval buttons
```

### Image generation

```bash
# Via the wrapper script (routes to correct mflux command automatically)
~/.mlx-ai-workstation/mlx-image.sh "a glowing robot in a neon city, cinematic"

# Custom settings
~/.mlx-ai-workstation/mlx-image.sh "PROMPT" out.png z-image-turbo 9 1024 1024

# Or use the Create page in the dashboard — model chips, style presets, gallery
```

### Text-to-speech

```bash
# Dashboard API (dashboard must be running at :8800)
curl -X POST http://localhost:8800/api/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello world", "voice": "af_sky", "speed": 1.0}'
# → {"file": "tts_<ts>.wav"}  saved to ~/MLX-AI/documents/audio/

# Available voices: af_sky, af_heart, am_adam, bm_lewis, bf_emma, am_echo
# Speed range: 0.5–2.0
```

### Video generation

```bash
# Dashboard API — fires an async job, returns job_id
curl -X POST http://localhost:8800/api/video \
  -F "prompt=cinematic drone shot over a misty mountain valley at sunrise" \
  -F "duration=5" \
  -F "model=ltx"
# → {"job_id": "abc12345", "status": "running"}

# Poll for completion
curl http://localhost:8800/api/video-job/abc12345
# → {"status": "done", "file": "vid_<ts>.mp4", "elapsed": 312}
# File saved to ~/MLX-AI/documents/videos/
```

---

## Project layout

```
local-ai-workstation/
├── mlx-setup.sh           ← single installer / manager (bootstrap, start, stop, status…)
├── mlx-agent.py           ← persona-aware tool-executor with spawn_subagent
├── mlx-telegram.py        ← Telegram bot bridge with live message updates
├── mlx-image.sh           ← image generation wrapper (mflux command routing)
├── dashboard/
│   └── app.py             ← Flask dashboard — backend API + 8 pages (/, chat, vision, create, audio, video, personas, models)
├── tests/
│   ├── test_dashboard.py  ← Playwright browser automation (30/30 checks)
│   └── screenshots/       ← per-page screenshots from test runs
└── screenshots/           ← README screenshots

~/.mlx-ai-workstation/     ← runtime workdir (never committed)
├── .env                   ← secrets
├── mlx-server.config.yaml ← 9-model MLX server config
├── litellm.config.yaml    ← role aliases + gateway settings
├── personas.json          ← agent persona registry
├── activity/
│   └── events.jsonl       ← append-only SSE source for live dashboard feed
├── agent/
├── dashboard/
└── logs/
```

---

## Agent tools

| Tool | What it does |
|---|---|
| `run_shell` | Execute shell commands (safe ones auto-run; others ask) |
| `read_file` | Read any file on disk |
| `write_file` | Write a file (confirms path) |
| `web_search` | Search via local SearXNG |
| `web_fetch` | Fetch a URL and extract text |
| `list_directory` | List a directory |
| `run_python` | Execute Python code |
| `spawn_subagent` | Delegate to a specialist persona (sequential) |
| `download_model` | Pull a new model from HuggingFace |
| `broadcast_event` | Emit an event to the dashboard SSE feed |
| `generate_image` | Generate an image locally via FLUX (mflux-generate-z-image-turbo) — no API |

`spawn_subagent` is the key tool for SDLC — each call fully loads a different model, runs the task, unloads, and returns the result to the orchestrator. `generate_image` lets any persona produce images without leaving the agent session.

---

## LiteLLM role aliases

Any OpenAI-compatible client can route by role instead of model name:

```
orchestrator    researcher    ba
coder           dev
writer          qa
reasoner        deep-reasoner
vision          designer
security        data          devops        architect
imagegen        thaqif-clone
embed
# bare model names also work: qwen36-35b, deepseek-r1-32b, etc.
```

---

## Management commands

```bash
./mlx-setup.sh --start             # start all services
./mlx-setup.sh --stop              # stop all services
./mlx-setup.sh --restart           # restart all
./mlx-setup.sh --status            # health check

./mlx-setup.sh --pull-core         # download core models
./mlx-setup.sh --pull-heavy        # download coder 80B + reasoner 70B
./mlx-setup.sh --add-model <repo> <role> <type>

./mlx-setup.sh --image "prompt"    # generate an image
./mlx-setup.sh --configure         # set secrets
./mlx-setup.sh --telegram          # start Telegram bot in foreground
./mlx-setup.sh --bootstrap         # full reinstall (keeps model cache)
```

---

## Troubleshooting

**Dashboard not loading**
```bash
tail -50 ~/.mlx-ai-workstation/logs/dashboard.log
launchctl kickstart -k gui/$(id -u)/com.mlx.dashboard
```

**MLX server not responding**
```bash
tail -50 ~/.mlx-ai-workstation/logs/mlx-inference.log
launchctl kickstart -k gui/$(id -u)/com.mlx.inference
```

**Model cold-start is slow (30–90 s)**
This is normal — the first inference after an idle period reloads from disk. On-demand idle timeout is 600 s for core models, 300 s for heavy/vision. The Telegram bot and dashboard both show a live "Loading model…" indicator during this time.

**RAM pressure with large models**
Only one large model loads at a time. If you trigger two simultaneously (CLI + dashboard), they compete. The sequential subagent design prevents this during SDLC pipelines. For interactive use, close one session before starting another.

**Telegram bot not responding**
```bash
launchctl list | grep telegram
tail -50 ~/.mlx-ai-workstation/logs/mlx-telegram.log
curl -s https://api.telegram.org   # network check
```

---

## Model licenses

| Model family | License |
|---|---|
| Qwen (all) | Apache 2.0 |
| DeepSeek-R1 Distill | MIT |
| Gemma-4 | Google Gemma Terms of Use |
| Z-Image Turbo | Tongyi Qianwen License (commercial OK) |

This setup code: **MIT**.
