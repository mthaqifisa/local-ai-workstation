# Local AI Workstation — Version 2

> A fully local, Telegram-driven AI development team running entirely on your Mac.
> No cloud APIs required. No subscriptions. No data leaving your machine.

---

## What This Is

Local AI Workstation v2 turns your MacBook Pro (Apple Silicon, 64 GB) into a private AI software studio. You talk to a main bot on Telegram. He manages a team of seven specialist agents — a product manager, a UI/UX designer, a developer, a QA tester, a security pentester, and a trend watcher — each backed by a carefully chosen local model. You approve every major decision before anything happens.

Everything runs on-device via Ollama. Docker hosts the web services. A custom Flask dashboard gives you a live Jira-style view of every project and agent.

---

## The Agent Team

| Agent | Role | Model | RAM | Notes |
|---|---|---|---|---|
| 🤖 **Orion** | Main Orchestrator | `qwen3:14b` | ~8 GB | Always loaded. Routes tasks, answers questions, drives workflow |
| 📊 **Ada** | Product Owner / PM | `qwen2.5:72b` | ~44 GB | Proposals, user stories, final sign-off |
| 🎨 **Mira** | UI/UX Designer | `gemma4:26b` | ~18 GB | Multimodal — can analyse screenshots and mockups |
| 💻 **Leo** | Developer | `qwen3-coder:30b` | ~22 GB | Any language, any stack |
| 🔎 **Nova** | QA / Tester | `qwen2.5:72b` | ~44 GB | Comprehensive test cases, bug reports |
| 🛡️ **Cipher** | Pentester | `qwen3-coder:30b` | ~22 GB | On-demand only, requires your confirmation |
| 📡 **Vox** | Trend Watcher | `qwen2.5:72b` | ~44 GB | Daily morning suggestions + on-demand |

**Memory at peak:** Orion (8 GB) + Ada/Nova/Vox (44 GB) = ~52 GB loaded simultaneously. OS and Docker take ~8 GB. Total ~60 GB out of 64 GB.

---

## Project Workflow

```
You (Telegram idea)
  └─► Ada + Mira write proposal
        └─► Your approval ✅
              └─► Leo builds & deploys
                    └─► Nova runs QA tests
                          ├─► Bugs found → Leo fixes → Nova retests (loop)
                          └─► All pass → Ada final review
                                └─► Your final approval ✅
                                      └─► Done 🎉

Cipher  — runs only when you explicitly say "pentest [target]"
Vox     — pings you every morning at 7 AM + available on-demand anytime
```

You are the **only approver**. No agent takes a major action without your confirmation via Telegram inline buttons.

---

## Prerequisites

- macOS on Apple Silicon (M-series)
- 64 GB unified memory (minimum 32 GB, some models won't fit)
- ~120 GB free disk space (models are large)
- macOS updated to latest (Apple menu → System Settings → Software Update)
- A Telegram account (free)

---

## Installation

### Step 1 — Clean up the v1 setup (if you ran it)

```bash
bash cleanup_v1-openClaw.sh
```

This removes the old launchd agents, Docker containers, Python venv, and configs. Your `.env` secrets and all Ollama models are **not touched**.

### Step 2 — Run the new setup

```bash
bash setup_ai_team.sh
```

The script is fully idempotent — safe to re-run at any time. Every step checks whether it already succeeded and skips it. You will be prompted for:

- **Telegram bot token** — from [@BotFather](https://t.me/BotFather) (free, 2 minutes to create)
- **Your Telegram chat ID** — from [@userinfobot](https://t.me/userinfobot) (needed for Vox daily messages)
- **Langfuse API keys** — optional, only needed if you want agent trace logs in the dashboard

The setup downloads ~60–70 GB of models on first run. Subsequent runs skip already-downloaded models.

---

## What Gets Installed

**System tools** (via Homebrew)
`ollama` · `colima` · `docker` · `node` · `git` · `jq` · `wget` · `lazydocker` · `uv` · `socat`

**Ollama models**
`qwen3:14b` · `qwen3-coder:30b` · `qwen2.5:72b` · `gemma4:26b` · `qwen3.6:27b` · `nomic-embed-text`

**Docker containers** (via Colima)
Open WebUI · SearXNG · Langfuse

**Python services** (in `~/ai-workstation/.venv`)
LiteLLM gateway · Flask dashboard · Telegram orchestrator bot · Trend watcher

**launchd agents** (auto-start at login)
`com.aiws.colima` · `com.aiws.litellm` · `com.aiws.dashboard` · `com.aiws.orchestrator` · `com.aiws.trendwatcher` · LAN bridges for Open WebUI, SearXNG, Langfuse

---

## Service URLs

All services are reachable from other devices on your Wi-Fi via your Mac's LAN IP.

| Service | URL | Purpose |
|---|---|---|
| **Dashboard** | http://localhost:8800 | Live Jira-style project boards + agent status |
| **Open WebUI** | http://localhost:3001 | Chat UI over your local models |
| **SearXNG** | http://localhost:8888 | Private web search (used by agents) |
| **Langfuse** | http://localhost:3000 | Agent call traces and logs |
| **LiteLLM** | http://localhost:4000 | Model routing gateway |
| **Ollama** | http://localhost:11434 | Local model server |

---

## Using the System

### Start a project

Just DM Orion (your Telegram bot) with a plain-English idea:

```
build me a habit tracker app with a React frontend and Node backend
```

Orion classifies it, Ada and Mira draft the proposal, and you approve or request changes via inline buttons. No commands needed.

### Ask a question

Anything that isn't a project idea gets answered directly by Orion:

```
what's the difference between REST and GraphQL?
```

### Get trend ideas

At any time (not just 7 AM):

```
what should I build?
```

Or use the command: `/trends`

### Run a pentest

```
pentest the login endpoint at localhost:3000
```

Cipher will always ask for your explicit confirmation before proceeding. **Only test systems you own or have written permission to test.**

### Manual IDE mode

When you want to code yourself in VS Code:

```
/pause
```

This frees the 30B model slot. Open VS Code → Continue extension → select **Leo Manual (qwen3.6:27b)**. When done:

```
/resume
```

The project workflow picks up exactly where it left off.

### Telegram commands

| Command | What it does |
|---|---|
| `/start` | Welcome message and team intro |
| `/status` | All agent statuses + current project state |
| `/projects` | List all your projects |
| `/trends` | Ask Vox for trend ideas right now |
| `/pause` | Pause agents, free model slot for VS Code |
| `/resume` | Resume agents and continue workflow |
| `/help` | Command reference |

---

## The Dashboard

Open `http://localhost:8800` in any browser on your network.

**Overview tab** — hardware metrics (CPU, RAM, storage, battery) and service health with live/down status for all six services.

**Agents tab** — seven agent cards showing name, role, backing model, and live status (idle / working). The card glows yellow while an agent is active.

**Projects tab** — per-project Kanban board. Each project you start in Telegram gets its own tab. The board has eight columns (Proposal → Awaiting Approval → Development → QA → Bugs Found → Final Review → Final Approval → Done) and the project card moves through them automatically.

**Activity tab** — last 20 agent calls from Langfuse (timestamp, agent name, latency). Requires Langfuse API keys to be configured.

The dashboard auto-refreshes every 5 seconds.

---

## File Structure

```
~/ai-workstation/
├── .env                        # Your secrets (chmod 600 — never commit this)
├── .venv/                      # Python virtualenv
├── litellm.config.yaml         # Model routing (Orion/Ada/Leo/etc → Ollama models)
├── start_gateway.sh            # LiteLLM gateway launcher
├── projects.json               # Project state machine (chmod 600)
├── agent_status.json           # Live agent working/idle states
├── agents/
│   ├── team.yaml               # All 7 agent system prompts and model assignments
│   ├── orchestrator.py         # Telegram bot — the brain of the operation
│   └── trend_watcher.py        # Daily Vox script (run by launchd at 7 AM)
├── dashboard/
│   └── app.py                  # Flask dashboard (Jira-style UI)
├── proposals/                  # Saved proposal Markdown files (per project)
├── logs/                       # launchd service logs
├── langfuse/                   # Langfuse docker-compose (git clone)
└── searxng/
    └── settings.yml            # SearXNG config
```

---

## Service Control

```bash
# Check what's running
bash setup_ai_team.sh --status

# Start everything
bash setup_ai_team.sh --start

# Stop everything (models and data preserved)
bash setup_ai_team.sh --stop

# Restart all services
bash setup_ai_team.sh --restart

# Update everything (Homebrew, models, Python packages, Docker images)
bash setup_ai_team.sh --update

# Full reset (removes configs and services, keeps models and .env)
bash setup_ai_team.sh --reset
```

---

## Troubleshooting

**Ollama not responding after setup**
Run `ollama serve` in a separate terminal, then re-run the setup script. The brew service sometimes needs a manual kick after first install.

**qwen2.5:72b feels slow**
This is expected — it's a 44 GB model. First load takes 30–60 seconds. Subsequent calls within the `OLLAMA_KEEP_ALIVE=3m` window are much faster. Orion's 14B responses are instant.

**Memory pressure (spinning beach ball)**
Close Chrome, Docker Desktop GUI (Colima runs headless so this is fine), and Xcode if open. The 72B model + macOS overhead is the tight spot. If it persists, edit `OLLAMA_KEEP_ALIVE=1m` at the top of `setup_ai_team.sh` and re-register the services.

**Telegram bot not responding**
Check `~/ai-workstation/logs/com.aiws.orchestrator.err.log`. Most common causes: `TELEGRAM_BOT_TOKEN` not set in `.env`, or the LiteLLM gateway isn't running yet (check `--status`).

**Model tag not found on `ollama pull`**
Ollama tags occasionally change. Verify at [ollama.com/library](https://ollama.com/library) and update the `MODELS` array and `litellm.config.yaml` in the setup script.

**Dashboard shows all services as DOWN**
The dashboard Flask app and launchd agents need a few seconds after boot. Wait 30 seconds then refresh. If it persists, run `--status` to see what's actually stopped.

**Docker containers not starting**
Colima may not have started yet. Run `colima start` manually, then `bash setup_ai_team.sh --start`.

---

## Security Notes

- Services bind `0.0.0.0` — reachable from any device on your Wi-Fi. This is fine on a trusted home network. On public Wi-Fi, change bindings to `127.0.0.1` in the relevant Docker run commands and launchd bridge agents.
- `.env` and `projects.json` are `chmod 600` — readable only by you.
- Cipher (pentester) always requires explicit confirmation before running. Never use it on systems you don't own.
- No data is sent to any cloud service unless you explicitly configure an OpenRouter key (not required, not included by default).

---

## Honest Limits

Local models are powerful but not identical to frontier cloud models. Expect:

- Ada's proposals to be solid but occasionally less polished than GPT-4-class output — the 72B model closes most of this gap.
- Leo's code to work well for common stacks (React, Node, Python, Go) but to need more guidance on niche frameworks.
- Nova's QA to catch real bugs but miss subtle security issues — that's what Cipher is for.
- Model load times of 30–60 seconds for the 72B on first call (subsequent calls within the keep-alive window are fast).

---

## License

MIT. Provided as-is, without warranty. Review all scripts before running — they install software, download models, and register background services.

---

*Built on: Ollama · LiteLLM · Open WebUI · SearXNG · Langfuse · python-telegram-bot · Flask · Colima*


echo "=== LITELLM ===" && tail -30 ~/ai-workstation/logs/com.aiws.litellm.err.log
echo "=== DASHBOARD ===" && tail -30 ~/ai-workstation/logs/com.aiws.dashboard.err.log


__if dashboard and gateway fail to start, run these:__
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