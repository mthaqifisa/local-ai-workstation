# LAW v6: Local AI Workstation

A private, free, open-source AI server for an Apple Silicon Mac (built and tested on an M5 Pro, 64 GB).
It runs strong open models locally for chat, judgement, coding, documents, image generation and editing,
speech and music, and serves them to every device on your network.

Quality comes first: answers go through a **draft → independent review → revise** loop where the reviewer
is a different model family from the writer, so the system never grades its own work.

## What you get

| Use it from | How |
|---|---|
| **Telegram** (main channel) | Message your bot: text, voice notes, photos; `/help` lists every command (`/image`, `/video`, `/music`, `/code`, `/project`, `/bugfix`, `/qa`, `/note`, …). Replies show live thinking |
| **Terminal** | `law ask "…"`, `law code "…"`, `law image "…"`, `law tts "…"`, `law stt file.wav`, `law music "…"`, `law jobs`, `law api <Class>`, `law qa <project>`, `law bugfix <project> [bug]`, `law board show <project>`, `law send <file>` |
| **Browser** (any device) | Open WebUI chat `http://<mac>.local:3000` · LAW Console `http://<mac>.local:8100` (live stats, agents, project boards, services, jobs, personas, settings) · dashboard `http://<mac>.local:3010` · Docker (Portainer, user `admin`, password `PORTAINER_ADMIN_PASSWORD` in `~/LAW/state/.env`) `http://<mac>.local:9000` |
| **Any OpenAI/Anthropic client** | `http://<mac>.local:8000/v1` |

Every request goes through one durable job queue with a scheduler that knows how much memory each model needs.
Big models are swapped in and out as needed, so you can use 30–45 GB models one at a time on 64 GB.

## Architecture

```mermaid
flowchart TB
  subgraph Clients["Your devices (LAN) and Telegram"]
    TG[Telegram app]:::c
    CLI[law CLI]:::c
    WEB[Browser]:::c
    APPS[OpenAI / Anthropic-compatible apps]:::c
  end

  subgraph Docker["Colima VM (4 CPU, 4 GB)"]
    OWUI[Open WebUI :3000<br/>chat · RAG · voice]
    SX[SearXNG :8888<br/>web search]
    HP[Homepage :3010]
    PT[Portainer :9000<br/>Docker management]
  end

  subgraph Mac["Native on Apple Silicon (Metal)"]
    BOT[Telegram adapter<br/>aiogram, long polling]
    API[law API :8100<br/>FastAPI · LAW Console]
    Q[(Valkey :6379<br/>durable job queue)]
    W[law worker<br/>model-aware scheduler]
    CADDY[Caddy :8000<br/>LAN front door]
    OMLX[oMLX :8001<br/>LLM · VLM · embed · rerank]
    MEDIA[mflux · mlx-audio · mlx-gen<br/>image · speech · music · video]
    AGENTS[OpenCode agents<br/>+ Mac tools MCP]
    CALC[run_python calculator<br/>numpy · scipy · sympy · pandas]
  end

  TG <--> BOT --> API
  CLI --> API
  WEB --> API
  WEB --> OWUI
  APPS --> CADDY
  API <--> Q <--> W
  W --> OMLX
  W --> MEDIA
  W --> AGENTS
  W --> CALC
  AGENTS --> CADDY
  OWUI --> CADDY --> OMLX
  OWUI --> SX
  classDef c fill:#eef3ff,stroke:#6d8cff,color:#111
```

*(Static image: [docs/images/architecture.png](docs/images/architecture.png))*

## How a request flows

```mermaid
sequenceDiagram
  autonumber
  actor You
  participant Tony as Telegram bot
  participant API as law API
  participant Q as Valkey queue
  participant W as Worker (scheduler)
  participant Q38 as Qwen3.8-27B (writer)
  participant G4 as Gemma 4 31B (judge)

  You->>Tony: "Monthly payment on 400k at 4.2% over 30 years?"
  Tony->>API: POST /jobs (ask, reply_to = your chat)
  API->>Q: store job (priority: interactive)
  W->>Q: claim next job (priority → resident model → most waiting work)
  W->>W: unload other large models if memory is needed
  W->>Q38: draft (bounded reasoning, run_python for every number)
  Q38-->>Tony: 💭 live thinking in the status message
  W->>G4: independent review — recompute figures, score 1-5
  alt score ≥ 4
    W-->>API: draft is the answer
  else score < 4
    W->>Q38: revise with the reviewer's issues
  end
  API-->>Tony: result → formatted reply (tables, bold, code)
```

*(Static image: [docs/images/request-flow.png](docs/images/request-flow.png))*

Coding tasks follow the same idea at project scale: **translate** (the request becomes a precise brief: goal,
requirements, acceptance criteria, assumptions) → **work** (coder agent) → **review** → **fix** if the review fails.
For projects, every review round has two independent checks, both on Gemma (another model family than the Qwen
coder):

1. **Black-box QA** tests the *product*, never the code. In a fresh session it starts the app as the README says,
   runs its regression pack (`<project>/qa/`), then checks every requirement and acceptance criterion through the
   app itself (headless browser, HTTP API, commands) and the edges a user hits. A guard stops it reading source
   files. Bugs go to `<project>/.law/BUGS.md` with steps, expected and actual result, evidence and a severity
   (blocker, major, minor); only QA can close a bug, after retesting it.
2. **Code review** reads the code against the spec and runs the project's checks.

A round passes only when no blocker or major bug is open and the code review passes (ADR-0018).

Every project keeps its working memory in **`<project>/.law/`** (like `.claude/` for Claude Code): `PROMPT.md` (the
brief), `REQUESTS.md` (every request and every note you sent the agent), `RESEARCH.md`, `PLAN.md` (steps with
checkboxes and gates), `PROGRESS.md` (done, gate output, next step), `BUGS.md` (QA's and your bug reports) and
`ENVIRONMENT.md` / `env.sh` (the tools the stack needs). `.law/.gitignore` is an allow-list: only these files are
ever committed, so a scratch file or a key an agent drops there cannot reach git. The agent keeps them current and continues from them, so a session that dies
halfway picks up where it stopped; a restarted job resumes its own agent session (`.law/session.json`, git-ignored).
With no folder given, the translator names the project folder (an existing one only when you name that project) and
LAW creates it directly under `~/SourceCode/`.

## The stack

| Layer | Component | Version | Licence | Why |
|---|---|---|---|---|
| LLM runtime | [oMLX](https://github.com/jundot/omlx) | 0.7.1 | Apache-2.0 | MLX on Metal; OpenAI **and** Anthropic APIs; batching; SSD KV cache; load/unload API |
| LAN front door | Caddy | 2.11 | Apache-2.0 | Exposes the LLM API on the LAN without a key (oMLX requires one off-loopback) |
| Queue | Valkey | 9.1 | BSD-3 | Durable jobs, leases, events; Redis-compatible |
| API / CLI / worker | LAW (`src/law`) | 6.0 | — | FastAPI, Typer, pydantic; model-aware scheduler; quality loop |
| Chat UI | Open WebUI | 0.12.3 | Open WebUI licence | Chat, RAG, voice, image tools for every device |
| Search | SearXNG | 2026.10.17 | AGPL-3.0 | Private meta-search for agents and chat |
| Agents | OpenCode | 1.18 | MIT | Coding/operator agents on local models; per-role configs |
| Images | mflux | 0.23 | MIT | Qwen-Image 2.1 generate + edit, SeedVR2 upscaling |
| Workflows | ComfyUI | 0.41.3 | GPL-3.0 | Node-based image/video workflows (Metal) |
| Video | mlx-gen | 0.38 | MIT | Wan 2.2 14B text-to-video and image-to-video |
| Speech & music | mlx-audio | 0.5.9 | MIT | Kokoro TTS, Whisper STT, Chatterbox cloning, ACE-Step songs |
| Telegram | aiogram | 3.31 | MIT | Async bot, long polling (no inbound port) |
| Containers | Colima + Docker CLI | 0.10 | MIT / Apache-2.0 | Lightweight VM for the web apps |

## Models (64 GB: one large model at a time, swapped by the scheduler)

| Role | Model | Loaded size |
|---|---|---|
| Reasoning, writing, vision, coding agent (default) | Qwen3.8-27B, 8-bit | 28 GB |
| Fast coder for short tasks (32k context) | Qwen3-Coder-Next 80B MoE, 4-bit | 41 GB |
| Fast / operator | Qwen3.6-35B-A3B, 8-bit | 36 GB |
| Independent judge | Gemma 4 31B, 8-bit (different family → no self-grading) | 34 GB |
| Embeddings / rerank | Qwen3-Embedding-4B · Qwen3-Reranker-0.6B | 3 GB |
| Images | Qwen-Image 2.1 (8-bit) | ~40 GB |
| Video | Wan 2.2 A14B T2V / I2V (8-bit) | ~42 GB |
| Speech / music | Kokoro · Whisper large-v3-turbo · Chatterbox fp16 · ACE-Step 1.5 | small – 20 GB |

Exact repos and pinned revisions: `settings/registry.yaml`. Decisions and trade-offs: `docs/adr/0002-model-roster.md`.

## How quality is enforced

- **Independent judge**: a different model family scores every answer against a rubric; weak drafts are revised.
- **No mental arithmetic**: roles that handle numbers must use the `run_python` calculator; the judge recomputes figures.
- **Bounded reasoning**: each role has a reasoning effort, so the model can't think itself out of an answer.
- **Agent work is reviewed**: code and documents get a read-and-run review by the judge and a fix pass; projects
  also get black-box QA of the running product every round (above).
- **Reviews are real**: the judge keeps most of its context for reading (output capped at 6K tokens), and a review
  that ends without findings runs again instead of being turned into a score.
- **Stuck agents are caught**: a writing agent that saves nothing for 25 minutes is stopped and resumed in the same
  session with a nudge (coders: build and fix the first error; writers: save the next section), up to 3 times. A
  long single generation gets up to 45 minutes; an agent busy reading without saving does not.
- **Nothing is lost when a step dies**: agents keep a to-do list and progress in `.law/`, write big files section by
  section, and LAW records a step in `.law/PROGRESS.md` itself when the agent did not. Instructions are looked up
  when each step runs, so retries and long projects always use the current rules.
- **Agents are kept on track**: an agent that keeps looking up APIs without writing code (`law api` research
  budget), reads library internals (jars, `node_modules`, a library's source on GitHub), or reads LAW's own source or
  state (secrets) is stopped and pointed to the supported way. Coders print the exact data when a test fails on data,
  and search an unfamiliar error's text before guessing.
- **You can steer a running agent**: `/note <project> <text>` reaches it at its next action.
- **Measured claims**: agents that benchmark LAW itself run with no other request on the model and check
  `~/LAW/logs/omlx.log`; documents are checked as a reader sees them (text extraction and page images).
- **Safe updates**: `law install` never restarts the worker mid-step; it restarts between steps.
- **House standard** in every role: answer first, sourced claims, no invented facts, units or currencies.
- **Measured**: `law eval` runs a verified golden set through the real product; `scripts/test_report.py` writes `docs/TEST-REPORT.md`.

## Security posture

Chosen by the owner for a home network: user-facing services are open to the LAN **without login**, and agents act with
the user's permissions (open apps, run commands). Telegram, being internet-reachable, answers only allow-listed user IDs.
Valkey and oMLX bind to loopback. Secrets live only in `~/LAW/state/.env` (mode 600). Don't run LAW on untrusted
networks (hotel or office Wi-Fi). See `docs/adr/0006-network-and-access.md`.

## Install

On a new or freshly formatted Mac with Homebrew and Python installed:

```bash
git clone <this repo> ~/SourceCode/OpenSourceLocalAI
cd ~/SourceCode/OpenSourceLocalAI/v6
./install.sh            # safe to run again any time
law doctor              # health check + the addresses to use from your other devices
```

The first run downloads about 200 GB of models. Later runs change only what differs from `settings/`.
The repo can live anywhere under any folder name. If you move or rename it after installing, run `./install.sh`
once from the new location: it points the `law` command, the services and your shell profile at the new place.

## Set up Telegram

LAW answers through a Telegram bot that you own. It only replies to the Telegram accounts you allow.

**The easy way: `./install.sh` asks you.** Whenever Telegram isn't set up and you run `./install.sh` in a Terminal, it:

1. Asks for the **bot token** (typing is hidden). Get it from **@BotFather** in Telegram:
   `/newbot` for a new bot, or `/mybots` → your bot (e.g. "Tony") → **API Token**.
2. Checks the token with Telegram and shows the bot's name, e.g. `Connected to @TonyBot`.
3. Asks you to **send the bot any message** from your phone or Mac, then press Enter.
   It lists who just messaged the bot (e.g. `1. Alex (@alex) [id 512345678]`); pick yourself (and anyone else to allow).
4. Saves both to `~/LAW/state/.env` (readable only by you, never in the repo) and starts the bot.
5. Asks for your **report email**: where LAW sends finished documents such as `/proposal` PDFs, through this
   Mac's Mail app (no mail password is stored). Change it any time in the **LAW Console → Settings**.

Press Enter at the first prompt to skip; it will ask again on the next run.
Before you start, make sure no other program is running the same bot (an old setup or another server):
a bot can only be connected to one program at a time.

Check it with `law doctor` (should show `Telegram: configured, 1 allowed id(s)`), then send the bot `hello`.

**Manual alternative** (scripts, or changing it later):

```bash
law secrets set LAW_TELEGRAM_TOKEN 123456789:AAH...
law secrets set LAW_TELEGRAM_USER_IDS 512345678            # several: 512345678,698765432
law install
```

**What you can send**

| Message | What happens |
|---|---|
| Any text | Answered on the fast model, remembering the conversation ("continue", "yes" carry on); long answers go through the quality loop (draft → independent review → revise). Work that needs files, commands or documents (a report, a PDF, building or fixing software, research) is handed to the right agent, which reports back in the same chat |
| Voice note | Transcribed, then answered |
| Photo (optionally with a caption) | Described or answered about, using the vision model |
| `/project init` | Start a **full project (SDLC)**: Tony sends a template (name, repo, repo URL, stack, thinking level, proposal depth `brief`/`full`, summary, details). Send it back filled in, and Tony researches and emails a proposal (PDF, also posted in Telegram), asks **Go / No go**, sets up the tools the stack needs, builds in milestones (a ticket board in `.law/PLAN.md`; a fresh agent session per ticket, and after each one LAW itself runs the project's whole gate, `.law/check.sh`, and hands any failure to the next session), tests and fixes until an independent review passes, writes a **system handover document** with real screenshots (emailed), then asks you to **verify**. **Needs changes** brings an enhanced proposal (v2, v3, …) and the loop repeats until **All good**. Decide with the buttons or `/go`, `/nogo`, `/changes`, `/allgood`; `/projects` shows where each one is; `/email <project>` resends its latest document |
| `/code init` | **Code only**: the same template without the proposal depth. Tony posts a short **plan in the chat** (what it will build, stack and versions, milestones, tests, assumptions) and asks Go / No go (No go with notes revises the plan). On Go: tools, milestones, tests, independent review, then your verification with how to run it. No research, PDFs or email; add documents later with `/proposal <folder>` |
| `/code <task>` | The coding agent builds a new project in its own folder under `~/SourceCode/` (named after the project you describe) |
| `/code <folder> <task>` | Works in that project, e.g. `/code ~/SourceCode/my-app add a login page`; a new folder name (e.g. `~/SourceCode/new-app`) is created |
| `/proposal [<folder>] <request>` | A consultant-grade proposal: web research with sources, then a full document (executive summary, market, solution, technical design, architecture, user and system flows, wireframes, delivery plan, cost, risks), independently reviewed, rendered to PDF and **emailed to you**. Give a project folder to present it as the working prototype |
| `/image <prompt>` | Generates an image and sends it back |
| `/music <request>` | A full song: theme, genre/vibe and length (e.g. `2 min`). Tony writes the lyrics, or add a `lyrics:` line with your own; say `instrumental` for no vocals |
| `/film <request>` | A short film (up to 2 min): shots planned from your theme or a `script:` section, rendered with fast Wan 2.2, scored and cut together. Say `no music` to skip the score |
| `/agent <role> <task>` | Runs a role, e.g. `/agent architect design a payments API` or `/agent operator play jazz on Spotify` |
| `/jobs [project or keyword]` | What's running and its health: 🟢 working, 🔵 thinking, 🟠 stalled/stuck, 🔴 failed. With a keyword: the step, last action, to-do progress and model activity, with **Retry / Skip / Restart / Cancel** buttons |
| `/retry <job>` | Run a failed job again from the step that failed (earlier work is kept). Every failed job also asks you **Retry / Skip** with buttons |
| `/note <project> <text>` | Tells the agent working on a project something while it works; it gets the note at its next action (also on the project's board) |
| `/bugfix <project> <what goes wrong>` | Reports a bug (in `.law/BUGS.md` and on the board). While the project builds, the working agent fixes it after its current step and QA retests it; otherwise the coder fixes it and QA retests the product, up to 3 rounds, and you get the result. `/bugfix <project>` alone fixes every open bug |
| `/qa <project>` | QA tests the running product now (black box, including the regression pack) and lists open bugs by severity. QA never runs on a schedule: only when you ask or inside a build you started. A new blocker is sent to you at once with its steps and screenshot |
| `/new` · `/hardreset` | Start a new conversation (Tony forgets the chat) · wipe all jobs, history and caches (asks first; keeps models, settings and projects) |

A status message updates live while the job runs. Messages from accounts that aren't on your list are ignored.

**Change or remove later:** run `law secrets set …` again, then `law install`. To move to a new Mac, `law secrets export ~/Desktop/law-secrets.env`
saves the token and IDs to a file you keep privately; on the new Mac run `./install.sh --secrets ~/Desktop/law-secrets.env`.

**Troubleshooting:** no reply → `law services logs telegram`. "Conflict: terminated by other getUpdates request" means another program
is still using the same bot (step 3).

## Use this Mac as an offline AI server for other machines

Everything LAW needs runs on this Mac, so another laptop on the same Wi-Fi can use its AI to write code **with no
internet at all** (for example a home router that isn't connected to the internet). Verified on 2026-10-20 by running
OpenCode in a sandbox with internet blocked: it wrote and tested code using only this Mac's model.

### Before you go offline (once, on the other machine, with internet)

1. Install **OpenCode** (the coding agent): `brew install opencode` (macOS/Linux) or `npm install -g opencode-ai` (any OS).
2. Install the tools your projects need (Python, Node, JDK, `pytest`, `npm install` in each project, …).
   Offline, the AI can still write code, but **nothing can be downloaded**, so dependencies must already be there.
3. Optional: VS Code with the **Continue** extension (see below), also installed while online.

### At home without internet

1. Connect both machines to the same Wi-Fi.
2. On this Mac, check the server is up and note its address:

   ```bash
   law doctor        # all "up"; the address is shown under "LAN access"
   ```

   The Mac's name is `<mac-name>.local` (e.g. `my-macbook.local`); it works without internet
   (Bonjour). If the name doesn't resolve, use the IP address from `law doctor` instead.
3. On the other machine, run the one-line setup this Mac serves (writes the OpenCode config and settings into your shell
   profile; safe to run again):

   ```bash
   curl -fsSL http://<mac-name>.local:8100/client/install.sh | sh
   ```

4. Open a **new** terminal on the other machine, go to your project and start the agent:

   ```bash
   cd ~/code/my-project
   opencode
   ```

   It uses this Mac's coding model (Qwen3.8-27B) and edits the files **on the other machine**.

5. Check, if anything seems off:

   ```bash
   curl http://<mac-name>.local:8000/v1/models     # lists the models: the network path works
   ```

### VS Code (Continue extension)

In Continue's config (`~/.continue/config.yaml`):

```yaml
models:
  - name: LAW Qwen3.8 (home server)
    provider: openai
    model: qwen3.8-27b-8bit
    apiBase: http://<mac-name>.local:8000/v1
    apiKey: local
    roles: [chat, edit, apply]
```

### Claude Code (optional)

Claude Code can also use this Mac's Anthropic-compatible endpoint. Install and start it once while online, then:

```bash
export ANTHROPIC_BASE_URL=http://<mac-name>.local:8000
export ANTHROPIC_AUTH_TOKEN=local
export ANTHROPIC_MODEL=qwen3.8-27b-8bit
export ANTHROPIC_SMALL_FAST_MODEL=qwen3.8-27b-8bit
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
claude
```

OpenCode is the recommended client offline: it is open source and the setup above was tested with internet blocked.

### Good to know

- The Mac holds **one large model at a time**. While it runs a heavy job (an agent, an image or video), a request for a
  *different* large model gets a "cannot load … memory" error; retry when `law jobs` shows it finished, or use the
  model that's already loaded.
- `law code` / `law agent` jobs (and Telegram `/code`) run on **this Mac's** files in `~/SourceCode`. To work on the
  other machine's files, use OpenCode/Continue there as above.
- The `law` command also works from the other machine for non-coding jobs (images, music, questions):
  `export LAW_SERVER=http://<mac-name>.local:8100` (set by the setup script) and install `law` with
  `uv tool install --python 3.12 <path-to-v6>` while online.
- No login is needed on the LAN (owner's choice); don't run LAW on untrusted networks.

## Software the agents build can use LAW

Apps the agents build may call LAW at runtime (the development rules tell them how; ADR-0018 and the dev spec):

- **LLM**: `http://<host>:8000/v1` (OpenAI-compatible) with LAW's fast model; from a Docker container the Mac is
  `host.docker.internal`. HTTP 507 means another model holds LAW's memory (e.g. an agent is working): busy, not an
  answer. The first call after idle loads the model (~10 s).
- **Web search**: SearXNG on port 8888.
- **Message the owner**: `law send --message "…"` on the Mac, or from a container
  `POST http://host.docker.internal:8100/send` with `{"text": "…"}` (Telegram, allow-listed chats only; no bot token
  in the project).

## Where things live

| Location | What | Survives a format? |
|---|---|---|
| `v6/` (git) | Code, `settings/` (models, ports, roles), `knowledge/`, `trained/`, docs | Yes, via git |
| `~/LAW/` | Models, caches, containers, generated media, logs, secrets (`state/.env`) | No, and that's fine |
| LAW Console → **Agents** | Live health of every running job (working / thinking / stalled / stuck / failed), its step, last actions, to-do progress and the model's activity, with Retry / Skip / Restart / Cancel | — |
| `<project>/.law/` | A project's brief, requests, research, plan, progress and bug list (commit it with the project) | With the project |
| `<project>/qa/` | QA's regression pack: black-box checks of the product, re-run every QA round (written by QA only) | With the project |
| LAW Console → **Projects** | A Jira-style board per project, built from what the agents record (stages and their logs, the coder's `PLAN.md` tickets and commits, `BUGS.md`): proposal, QA, development, bug fixes, your approvals, with notes | — |
| `<project>/docs/proposal/` or `~/LAW/outputs/docs/` | `/proposal` research, Markdown source, diagrams, wireframes, PDF | With the project |

Disk use is bounded: the LLM's SSD prompt cache is capped by `machine.llm_ssd_cache_gb` in
`settings/registry.yaml` (default 20 GB), agent build caches (Maven, npm, Gradle, pip, uv) go to `~/LAW/cache`,
and `law reclaim` removes retired models, stale downloads and old logs. History is kept for 14 days by default
(`retention:` in the registry: finished jobs, agent conversations, logs, trash), pruned every 6 hours. The
Console's **Storage** tab shows every cache with its size and what clearing it costs, clears them with a
button, and edits the retention and the prompt-cache limit.

Things you create in Open WebUI (model presets, prompts, tools, functions, knowledge-base definitions) are kept
in the repo with `law save`, which writes `settings/openwebui/`, refuses to save if it finds anything that
looks like a secret, and commits locally. `law install` on another Mac adds them back (`law restore-state` does
it on demand). Valves, chats, users and uploaded documents are not exported.

No secrets are ever stored in the repo. To reset everything machine-local: `law uninstall && rm -rf ~/LAW`.

## Docs

- [Quick start](docs/QUICKSTART.md): first 10 minutes
- [Runbook](docs/RUNBOOK.md): operate, update, troubleshoot
- [Architecture](docs/ARCHITECTURE.md) and [decision records](docs/adr/)
- [Research](RESEARCH.md): why these models and tools
