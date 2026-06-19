# Local AI Development Team — Version 2

A fully local, Telegram-driven multi-agent software development team on Apple Silicon Mac. You talk to **Orion** via Telegram — he runs your Mac, answers questions, and orchestrates a team of AI specialists to design, build, test, and review software. Everything runs on your machine. Nothing leaves it.

---

## Scripts

| File | Purpose |
|---|---|
| `cleanup_v1-openClaw.sh` | Safely removes the old v1 / OpenClaw setup |
| `setup_ai-team.sh` | Installs and configures the full AI team |

Run the cleanup script first if you had an earlier setup, then run the main setup script.

---

## Requirements

- macOS on **Apple Silicon** (M-series chip)
- **64 GB unified RAM** — the model lineup is sized for this
- ~120 GB free disk space (models are large)
- A [Telegram](https://telegram.org) account
- Internet connection for first-run downloads (all inference is local after that)

---

## Quick Start

```bash
# Step 1 — remove the old setup (skip if you're starting fresh)
bash cleanup_v1-openClaw.sh

# Step 2 — build the team
bash setup_ai-team.sh
```

Both scripts are **safe to run multiple times**. Every step checks before acting — nothing is overwritten unless it needs to be.

---

## `cleanup_v1-openClaw.sh`

Removes the v1 / OpenClaw installation so the new setup starts clean.

**What it removes:**
- All `com.aiws.*` and `com.openclaw.*` launchd agents (unloaded and deleted)
- Docker containers: `open-webui`, `searxng`
- Langfuse Docker containers — **volumes are kept**, so your trace history survives
- Python virtualenv (`~/ai-workstation/.venv`)
- Old configs: `litellm.config.yaml`, `start_gateway.sh`, `agents/`, `dashboard/`
- Continue IDE config (`~/.continue/config.yaml`)
- OpenClaw npm package (if installed)

**What it keeps:**
- Your secrets file (`~/ai-workstation/.env`) — tokens are never touched
- All Ollama models — no re-downloading required after cleanup
- Colima, Docker, and all Homebrew packages
- Langfuse Postgres database volumes

**Usage:**
```bash
bash cleanup_v1-openClaw.sh
```

The script will show exactly what it's about to do and ask for confirmation before proceeding.

---

## `setup_ai-team.sh`

Installs, configures, and registers the entire AI team as background services.

### What gets built

```
You (Telegram)
    │
    ▼
Orion — orchestrator, always on
    ├── Ada    — PM / Product Owner
    ├── Mira   — UI/UX Designer
    ├── Leo    — Developer
    ├── Nova   — QA Tester
    ├── Cipher — Pentester (on-demand only)
    └── Vox    — Trend Watcher (daily 7 AM + on-demand)
```

### Installation phases

| Phase | What happens |
|---|---|
| 0 — Preflight | macOS / Apple Silicon check, disk space check |
| 1 — Tools | Xcode CLT, Homebrew, core packages (`ollama`, `colima`, `uv`, `node`, etc.) |
| 2 — Models | Pulls all Ollama models (see lineup below) |
| 3 — Python | Creates virtualenv with LiteLLM, Flask, python-telegram-bot, and dependencies |
| 4 — Docker | Starts Colima, Open WebUI, SearXNG, Langfuse, Portainer |
| 5 — LiteLLM | Writes `litellm.config.yaml` and `start_gateway.sh` |
| 6 — Agent Team | Writes `team.yaml`, `orchestrator.py`, `trend_watcher.py`, plugin system |
| 7 — Dashboard | Builds the Jira-style web dashboard |
| 8 — Continue | Configures the VS Code / JetBrains IDE integration |
| 9 — Services | Registers all launchd agents (auto-start on login) |
| 10 — Tokens | Prompts for Telegram bot token and chat ID |
| 11 — Summary | Prints URLs, access points, and a status check |

### Model lineup

| Agent | Model | RAM | Notes |
|---|---|---|---|
| Orion | `qwen3:14b` | ~8 GB | Always loaded |
| Leo | `qwen2.5-coder:72b` | ~44 GB | On demand |
| Cipher | `qwen2.5-coder:72b` | — | Shares Leo's slot |
| Ada | `qwen2.5:72b` | ~44 GB | On demand |
| Nova | `qwen2.5:72b` | — | Shares Ada's slot |
| Vox | `qwen2.5:72b` | — | Shares Ada's slot |
| Mira | `gemma4:26b` | ~18 GB | Multimodal — can see images |
| Manual IDE | `qwen3.6:27b` | ~22 GB | Only loads during `/pause` |
| Embeddings | `nomic-embed-text` | ~270 MB | Always available |

**Peak RAM:** Orion + one specialist at a time ≈ 52 GB. Well within 64 GB.

### Service ports

| Service | Port | Purpose |
|---|---|---|
| Ollama | 11434 | Local model server |
| LiteLLM Gateway | 4000 | Named agent aliases + Langfuse tracing |
| Dashboard | 8800 | Jira-style project board |
| Open WebUI | 3001 | Browser chat UI |
| SearXNG | 8888 | Private web search for agents |
| Langfuse | 3000 | Agent trace logs |
| Portainer | 9001 | Docker container manager |

### Workspace directories

```
~/ai-workstation/          ← scripts, configs, .env, virtualenv
    agents/
        team.yaml          ← all agent definitions and system prompts
        orchestrator.py    ← Orion Telegram bot
        trend_watcher.py   ← Vox daily scheduler
        plugins/           ← self-written capability plugins
    dashboard/
        app.py             ← Flask dashboard server
    .env                   ← secrets (chmod 600, never committed)
    .venv/                 ← Python virtualenv

~/AI/                      ← all AI-generated content
    projects/<name>/       ← Leo's code + Nova's QA reports
    proposals/<name>/      ← Ada + Mira proposal documents
    screenshots/           ← /screenshot captures
    reports/               ← Cipher pentest reports
    trends/                ← Vox daily suggestions
```

---

## Telegram Commands

| Command | What it does |
|---|---|
| `/start` | Welcome message and team roster |
| `/status` | Active project state + agent status (idle / working) |
| `/projects` | List all your projects |
| `/trends` | Ask Vox for project ideas right now |
| `/pause` | Pause agents and free the model slot for manual VS Code coding |
| `/resume` | Resume the paused workflow |
| `/run <command>` | Execute a shell command on the Mac (asks confirmation) |
| `/screenshot` | Full Mac screenshot, sent to chat |
| `/screenshot <port>` | Screenshot of a local web service (e.g. `/screenshot 8800`) |
| `/files [path]` | List files in a directory |
| `/upgrade <thing>` | Ask Orion to research and write a plugin for a new capability |
| `/clear` | Clear conversation history for your chat |
| `/help` | Full command reference |

**Beyond commands** — just talk to Orion naturally:

- *"Build me a habit tracker app"* → starts the full workflow
- *"What's the weather in KL?"* → Orion searches and answers
- *"Open Spotify"* → opens the app
- *"What's my disk space?"* → answers immediately
- *"Pentest localhost:3000"* → asks for your confirmation before Cipher acts

---

## Project Workflow

```
You send an idea
    │
    ▼
Ada writes proposal + Mira writes design brief
    │
    ▼
You approve / request changes / reject  ← Telegram buttons
    │
    ▼
Leo builds the project (any stack)
    │
    ▼
Nova runs QA tests
    ├── Bugs found → Leo fixes → Nova retests
    └── All pass   → Ada does final PM review
                           │
                           ▼
                   You give final approval
                           │
                           ▼
                      ✅ Project complete
```

Cipher (pentesting) only activates on your explicit command and always shows a confirmation button before running.

Vox sends trend suggestions every morning at 7 AM. Call `/trends` any time for ideas on demand.

---

## Manual IDE Mode

When you want to code manually in VS Code or a JetBrains IDE using the Continue plugin:

```
/pause          ← frees the 72B model slot
                   Continue now uses Leo Manual (qwen3.6:27b)
... code away ...
/resume         ← restores the workflow where it left off
```

---

## Service Control

```bash
# Status check
bash ~/ai-workstation/setup_ai-team.sh --status

# Start / stop / restart all services
bash ~/ai-workstation/setup_ai-team.sh --start
bash ~/ai-workstation/setup_ai-team.sh --stop
bash ~/ai-workstation/setup_ai-team.sh --restart

# Update packages and model tags
bash ~/ai-workstation/setup_ai-team.sh --update

# Nuke everything and start fresh (keeps .env and models)
bash ~/ai-workstation/setup_ai-team.sh --reset

# Manually restart individual launchd agents
for svc in com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator; do
  launchctl unload ~/Library/LaunchAgents/$svc.plist
  launchctl load   ~/Library/LaunchAgents/$svc.plist
done
```

### Self-healing virtualenv

If Python packages get corrupted, the repair script runs automatically before each service starts. You can also trigger it manually:

```bash
bash ~/ai-workstation/repair_venv.sh
```

---

## Dashboard

Open **http://localhost:8800** in any browser.

- **Overview** — hardware metrics (CPU, RAM, storage, battery), service health grid, model roster
- **Agents** — live status cards for all 7 agents (idle / working)
- **Projects** — Jira-style Kanban board per project, document browser, activity timeline, ticket tracking
- **Activity** — Langfuse agent trace log

The dashboard polls every 5 seconds. No page reload needed.

---

## Configuration

All secrets live in `~/ai-workstation/.env` (permissions: `600`):

```
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=987654321
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000
```

You can override defaults before running the setup script:

```bash
WORKDIR=~/my-ai        bash setup_ai-team.sh   # custom install dir
AI_WORKSPACE=~/Dev/AI  bash setup_ai-team.sh   # custom project workspace
VOX_HOUR=8             bash setup_ai-team.sh   # Vox sends trends at 8 AM instead of 7
COLIMA_MEM=12          bash setup_ai-team.sh   # give Docker more RAM
```

---

## Troubleshooting

**Orion doesn't respond in Telegram**
```bash
launchctl list | grep aiws
tail -f ~/Library/Logs/aiws-orchestrator.log
```

**A model fails to load**
```bash
ollama list
ollama pull qwen2.5:72b    # re-pull if missing or corrupted
```

**Ollama using too much memory**
```bash
# In ~/.zprofile — reduce to 1 model at a time
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_KEEP_ALIVE=3m
brew services restart ollama
```

**Docker / Colima not starting**
```bash
colima start --cpu 4 --memory 8 --disk 60
docker ps
```

**Dashboard shows services as down**
```bash
bash ~/ai-workstation/setup_ai-team.sh --status
bash ~/ai-workstation/setup_ai-team.sh --start
```

**LiteLLM gateway not routing**
```bash
tail -f ~/Library/Logs/aiws-litellm.log
launchctl unload ~/Library/LaunchAgents/com.aiws.litellm.plist
launchctl load   ~/Library/LaunchAgents/com.aiws.litellm.plist
```

---

## Privacy

- All inference runs locally via Ollama — no prompts or responses leave your machine.
- Web searches go through SearXNG locally — queries are not tracked or logged externally.
- The only external connections are the Telegram Bot API (to receive and send your messages) and the initial model downloads from ollama.com.
- Secrets are stored in `.env` with `chmod 600` and are never included in logs.

### Troubleshoot
__If dashboard and gateway fail to start, run these:__
```bash
rm -rf ~/ai-workstation/.venv 
cd ~/ai-workstation 
uv venv --python 3.12 .venv
uv pip install --python ~/ai-workstation/.venv/bin/python 'litellm[proxy]' openai langfuse python-dotenv flask requests rich psutil "python-telegram-bot>=21.0" pyyaml 

~/ai-workstation/.venv/bin/python -c "import litellm, flask, requests, psutil, telegram, yaml; print('✅ venv OK')"

launchctl unload ~/Library/LaunchAgents/com.aiws.litellm.plist
launchctl unload ~/Library/LaunchAgents/com.aiws.dashboard.plist
launchctl load ~/Library/LaunchAgents/com.aiws.litellm.plist
launchctl load ~/Library/LaunchAgents/com.aiws.dashboard.plist

```

__Reset agent configuration by running these commands:__
```bash
rm ~/ai-workstation/agents/orchestrator.py
bash setup_ai_team.sh

launchctl unload ~/Library/LaunchAgents/com.aiws.orchestrator.plist
launchctl load  ~/Library/LaunchAgents/com.aiws.orchestrator.plist
```