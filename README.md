# Local AI Development Workstation

A one-command setup that turns a Mac (Apple Silicon, ideally 64 GB+) into a fully
local, private AI development workstation. Everything runs on your own hardware —
no API keys, no cloud inference, no data leaving your machine.

The agent **team** (Orion, Ada, Mira, Leo, Nova, Cipher, Vox) runs inside
**OpenClaw**. The setup script only provisions the infrastructure those agents
use. See [Setting Up Your Team in OpenClaw](#setting-up-your-team-in-openclaw)
for the copy-paste prompt.

---

## What the script installs

| Component | What it is | Port |
|---|---|---|
| **Ollama** | Local model engine (runs the LLMs) | `11434` |
| **LiteLLM Gateway** | OpenAI-compatible proxy in front of Ollama — this is what OpenClaw talks to | `4000` |
| **OpenClaw** | The phone-driven agent runtime where your team lives | `18789` |
| **Open WebUI** | Browser chat UI for the models | `3001` |
| **SearXNG** | Private metasearch engine (gives agents web search) | `8888` |
| **Langfuse** | Tracing / observability for LLM calls | `3000` |
| **Portainer** | Docker container management UI | `9001` |
| **Dashboard** | Live status page (system, services, models) | `8800` |
| **IDEs** | VS Code + IntelliJ IDEA | — |

Everything binds to `0.0.0.0`, so you can reach any service from your phone or
another computer on the same network using the Mac's LAN IP (shown on the
dashboard and in the setup summary).

---

## Models

### Core models (pulled automatically)

| Model | Role | Approx size |
|---|---|---|
| `qwen3.6:35b-a3b` | Orchestration / coordination | ~26 GB |
| `qwen3-coder:30b-a3b-q4_K_M` | Primary coder | ~18 GB |
| `gemma4:26b` | UI/UX, design, vision | ~18 GB |
| `qwen2.5:72b` | Heavy reasoning (PM, QA, security) | ~44 GB |
| `nomic-embed-text` | Embeddings (RAG / search) | ~270 MB |

### Swappable models (pull on demand)

Run `./setup.sh --pull-models` to interactively pull any of these. They're all
free, open-weight, and locally runnable. They're pre-registered in the LiteLLM
gateway, so once pulled you can reference them by name from OpenClaw immediately.

**Coding** — `qwen2.5-coder:32b` (accuracy leader), `qwen2.5-coder:72b`,
`qwen3.6:27b`, `devstral:24b` (best agentic), `deepseek-coder-v2:16b`,
`codestral:22b`

**Reasoning** — `mistral-small:24b` (lowest hallucination), `deepseek-r1:14b`,
`deepseek-r1:32b`, `deepseek-r1:70b`, `phi-4:14b`, `glm-4.7-flash`

**General** — `llama3.3:70b`, `mistral:7b`, `gemma4:31b`

> On a 64 GB machine, `OLLAMA_MAX_LOADED=1` keeps exactly one model hot at a
> time. Switching between, say, the 72B reasoner and a 70B model triggers a
> ~30-second reload. That's expected.

---

## Quick start

```bash
chmod +x setup.sh
./setup.sh --bootstrap
```

Before running, make sure:
- **OneDrive is installed and signed in** (documents go to `~/OneDrive/AI-Agent`;
  source code goes to `~/SourceCode`, kept out of OneDrive).
- You have a **Telegram bot** ready (create one via @BotFather; the script will
  prompt for the token and your chat ID).
- You have **~150 GB free disk** and ideally **64 GB RAM**.

### Control commands

```bash
./setup.sh --status        # live health of all services
./setup.sh --start         # start everything
./setup.sh --stop          # stop everything
./setup.sh --restart       # stop then start
./setup.sh --pull-models   # interactively pull optional models
./setup.sh --uninstall     # full rollback
```

---

## How it fits together

```
   Your phone (Telegram)
            │
            ▼
   ┌──────────────────┐      ┌──────────────────┐
   │     OpenClaw     │◄────►│  LiteLLM Gateway │  (OpenAI-compatible :4000)
   │  (your AI team)  │      └────────┬─────────┘
   └──────────────────┘               │
            │                         ▼
            │                  ┌──────────────┐
            │                  │    Ollama    │  (local models :11434)
            │                  └──────────────┘
            ▼
   SearXNG (search) · Langfuse (tracing) · Open WebUI (chat) · Portainer (docker)
```

- **OpenClaw** is the brain. You talk to it on Telegram. It coordinates the team,
  runs tools, and calls models **through the LiteLLM gateway**.
- **LiteLLM** gives every model a clean OpenAI-compatible name (`coder`,
  `reasoner`, `orchestrator`, etc.) so you can swap the underlying model without
  touching OpenClaw.
- **Ollama** does the actual inference, fully offline.

---

## Setting Up Your Team in OpenClaw

After `--bootstrap` finishes and you've run `openclaw onboard --install-daemon`,
open the OpenClaw chat (`http://<your-ip>:18789/chat?session=main` or via
Telegram) and **paste the prompt below as your first message**. It instructs
OpenClaw to create the seven-agent team and tells each agent which model to use.

> The model names in the prompt (`orchestrator`, `coder`, `reasoner`, `designer`)
> are the LiteLLM aliases defined in `~/.local-ai-workstation/litellm.config.yaml`.
> You can point any agent at a swappable model by using its alias instead
> (e.g. `coder-qwen25-32b` for maximum coding accuracy).

### OpenClaw Team Setup Prompt

```text
You are the coordinator of a local AI software team running on my Mac. Set up and
remember the following team of sub-agents. For each agent, use the exact model
name given (these are served by my LiteLLM gateway at http://0.0.0.0:4000/v1).
When I describe a project, run the workflow described at the end.

WORKSPACE RULES (apply to every agent)
- Save all source code under: ~/SourceCode/<project_name>/
- Save all documents (proposals, reports) under: ~/OneDrive/AI-Agent/<category>/<project_name>/
- Never invent file paths, commands, or APIs. If unsure, say so.
- Web search is available via SearXNG at http://0.0.0.0:8888/search?q=<query>&format=json

THE TEAM

1) ORION — Chief of Staff & Coordinator
   model: orchestrator
   role: You are the lead. You talk to me on Telegram, turn my ideas into a brief,
   decide which teammate handles what, and report progress. Keep replies concise
   and technical. You never write final code or proposals yourself — you delegate.

2) ADA — Product Owner / Project Manager
   model: reasoner
   role: Turn a brief into an Agile proposal with user stories, acceptance criteria,
   architecture, and milestones. Output clean Markdown. Do the final sign-off review
   when a project is complete.

3) MIRA — Senior UI/UX Designer
   model: designer
   role: Produce user journeys and wireframes. Output each wireframe as inline SVG
   inside a fenced code block headed with its filename, e.g.
   "### assets/wireframe_home.svg" then an ```svg block. Keep SVGs self-contained.

4) LEO — Senior Full-Stack Developer
   model: coder
   role: Implement the approved plan. Output complete, runnable code files, each in
   a fenced block headed by its path, e.g. "### src/app.py". Include a README with
   build/run steps. End with the line: DEPLOYMENT COMPLETE.

5) NOVA — QA Engineer
   model: reasoner
   role: Review Leo's output and run end-to-end test scenarios (Puppeteer is
   available). If everything passes, output: ALL TESTS PASSED. Otherwise file bug
   tickets in the form: [BUG-001] Title | Severity | Reproduction steps.

6) CIPHER — White-Hat Security Auditor
   model: reasoner
   role: Audit Leo's code for vulnerabilities (injection, auth, secrets, unsafe
   deps). Output a short risk-ranked findings list with concrete fixes.

7) VOX — Opportunity Scout
   model: reasoner
   role: On request (or daily), search current tech/startup news via SearXNG and
   propose 3 concrete project ideas. For each: problem, solution, first build step.

DEFAULT PROJECT WORKFLOW
When I say something like "build X":
  1. ORION writes a short brief and a project title.
  2. ADA writes the proposal; MIRA adds wireframes. Show me both, then ask me to
     approve before any coding.
  3. On approval, LEO implements and saves the code. 
  4. NOVA tests. If bugs are found, route back to LEO with the tickets, then re-test.
  5. CIPHER does a security pass.
  6. ADA writes the final sign-off. Show me the result and where files were saved.
Always pause for my approval at step 2 and at the final sign-off. Run the long
generation steps in the background and ping me when each artifact is ready.

Confirm you've registered the team and list each agent with its model.
```

### Switching a model for an agent

Tell OpenClaw, for example:

> "Use `coder-qwen25-32b` for Leo from now on — I want maximum coding accuracy."

or

> "Switch Nova and Cipher to `reason-deepseek-r1-32b` for deeper analysis."

Any alias from `litellm.config.yaml` works. Pull it first with
`./setup.sh --pull-models` if it isn't installed yet.

---

## Troubleshooting

- **A model won't pull ("file does not exist")** — the tag changed. Browse
  [ollama.com/library](https://ollama.com/library) for the current name. A failed
  pull only logs a warning; it never breaks the setup.
- **Services show "down" on the dashboard** — run `./setup.sh --restart`. Docker
  containers are recreated automatically if missing.
- **Can't reach services from your phone** — confirm the Mac's LAN IP (top of the
  dashboard) and that both devices are on the same network.
- **70B model is slow / swapping** — that's `OLLAMA_MAX_LOADED=1` doing a model
  swap. Only one large model stays resident at a time on 64 GB.

---

## Notes

- All inference is local and private. Nothing is sent to external model providers.
- OpenClaw connects to models **only** through the LiteLLM gateway, so observability
  (Langfuse) and model-swapping work uniformly.
- The setup script is idempotent — re-running `--bootstrap` checks each tool and
  only installs what's missing.
