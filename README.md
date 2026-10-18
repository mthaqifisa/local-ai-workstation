# Local AI Workstation v2 (`law`)

A one-command, fully local, open-source AI workstation for Apple Silicon Macs.
Branch `v5-enhanced-mlx` · version `2.0.0-phase7` · built for a 64 GB M-series MacBook, works on 32 GB+.

- **One installer** (`./install.sh`) for a brand-new Mac *or* a Mac that already has
  something installed. Every run checks everything: installs what's missing, repairs what
  broke, removes what's no longer needed. Run it as often as you like.
- **Agents that do the work.** A persona gets real tools — shell, files, web search, web
  pages, code search, **machine/GUI control** (open apps, AppleScript, mouse/keyboard via
  cliclick), **sight** (screenshot a running app and *look at it* with a vision model),
  memory — and hands sub-tasks to other personas. A quality loop (coder → QA → designer →
  product-owner) loops until the result is genuinely good, verifying the real rendered UI.
- **Create anything.** Text-to-image, text-to-video (with audio), text-to-speech, music
  (ACE-Step), and image understanding / OCR — all local, from the dashboard or Telegram.
- **A real command center.** A live web dashboard (`:8800`): task board, agent timelines,
  Create/Studio media tools, RAG file search, a read-only SQL viewer, a system monitor, and
  **usage analytics** (jobs & compute over time).
- **From your phone, naturally.** A private Telegram bot you can *talk to*: type, send a
  **voice note** (transcribed), or a **photo** (analysed). It answers like a modern
  assistant — shows its thinking live, remembers the conversation, **looks things up on the
  web** for current facts, formats replies richly, generates & sends back media, and runs
  **scheduled / daily tasks** (`/remind`, `/daily`).
- **From your editor.** OpenCode, Claude Code, Open WebUI and any OpenAI- or
  Anthropic-compatible app use the same models through the gateway.
- **One queue, one worker, one model in RAM.** Every request becomes a job. Jobs run one at
  a time, so a big model always gets the whole machine.
- **Never stuck.** Timeouts on every step, a watchdog that restarts frozen services, a
  memory guard that won't load a model when the Mac is about to swap, long jobs that recover
  and continue instead of dying, and clear reasons when something fails.
- **Undo.** Every agent run starts with a snapshot of its project folder; roll back in one
  command (or `/undo` from Telegram).
- **LAN / VPN access, no login.** Services listen on `0.0.0.0` on purpose.

> **Status:** working now — installer, model downloads, web UIs, the `:8800` command-center
> dashboard (+ usage analytics), job queue, worker, OpenAI + Anthropic gateway, CLI,
> watchdog, memory guard, agents (tools incl. machine-control + vision `look_at`, quality
> loop, request translator, sub-jobs, auto-continue, resume, project memory), media
> generation (image / video / voice / music), image understanding, RAG file search
> (`law index` / `law search`), and a conversational Telegram bot (text / voice / photo,
> live thinking, web-grounded answers, rich text, media in/out, reminders & daily tasks).

---

## Contents
1. [Requirements](#requirements)
2. [Install](#install)
3. [Quick start](#quick-start)
4. [Agents](#agents)
5. [Telegram](#telegram)
6. [OpenCode](#opencode)
7. [All commands](#all-commands)
8. [Using it from other apps (API)](#using-it-from-other-apps-api)
9. [Claude Code and other Anthropic clients](#claude-code-and-other-anthropic-clients)
10. [Web UIs and ports](#web-uis-and-ports)
11. [Models and personas](#models-and-personas)
12. [Adding and removing your own models](#adding-and-removing-your-own-models)
13. [How a job runs](#how-a-job-runs)
14. [Reliability: watchdog, memory guard, interruptions](#reliability-watchdog-memory-guard-interruptions)
15. [Long prompts and the 300-second limit](#long-prompts-and-the-300-second-limit)
16. [Measuring models: `law probe`](#measuring-models-law-probe)
17. [Undo: snapshots and rollback](#undo-snapshots-and-rollback)
18. [What the installer does](#what-the-installer-does)
19. [Files and folders](#files-and-folders)
20. [Configuration](#configuration)
21. [Moving from v1 (mlx-setup.sh)](#moving-from-v1-mlx-setupsh)
22. [Troubleshooting](#troubleshooting)
23. [Uninstall](#uninstall)
24. [Development and tests](#development-and-tests)
25. [Roadmap](#roadmap)

---

## Requirements

| | |
|---|---|
| Mac | Apple Silicon (M1 or newer). Intel Macs are not supported. |
| macOS | 14 Sonoma or newer |
| RAM | 32 GB minimum; 64 GB recommended (the 42 GB coder model needs it) |
| Disk | ~100 GB free for the default models, plus room for optional ones |
| Network | Needed for the first install (Homebrew, Python packages, models, Docker images) and for Telegram |
| Account | A normal user account — do **not** run with `sudo` |

Everything else (Xcode Command Line Tools, Homebrew, uv, Python 3.12, Colima, Docker, git…)
is installed for you.

---

## Install

### From a downloaded copy / git checkout
```bash
cd law            # the folder that contains install.sh
./install.sh
```

### One-liner on a fresh Mac
```bash
curl -fsSL https://raw.githubusercontent.com/mthaqifisa/local-ai-workstation/v5-enhanced-mlx/install.sh | bash
```
This clones the repo into `~/.law/src` and runs from there. Re-running the one-liner
updates that copy.

### What to expect on the first run
- A macOS dialog may ask to install the **Command Line Tools** — click Install.
- **Homebrew** may ask for your Mac password once (it also installs Chromium for the agents'
  screenshot/vision tools, unless you already have Chrome/Brave).
- **~86 GB of core models** download (the chat/coder/vision/embed models). Media models
  (image ~31 GB, video ~28 GB, voice ~2 GB) download lazily the **first time you use that
  feature**, so a brand-new Mac ends up identical to a long-running one — just spread out.
  Safe to interrupt (Ctrl+C) — the next run resumes.
- At the end you get a summary like `12 added · 3 updated · 0 removed`.

### Reproduce the full workstation (clone → run → done)
On a brand-new Mac, after `./install.sh` you have the whole system — agents, media generation,
vision, RAG, the dashboard, the gateway. A few features need a one-time, per-user step:

| To enable | Run / do once |
|---|---|
| **Telegram bot** (chat / voice / photos from your phone) | `law configure` — paste your bot token + user id |
| **Music generation** (ACE-Step) | `law setup-music` |
| **Agents sending email** | Add an account in **Mail.app**, and approve the one-time *Automation → control Mail* prompt |
| **LAN access** from other devices | set `network.bind_host: 0.0.0.0` in the registry — **read the security note below first** |
| **OpenCode / editor** integration | `law setup-opencode --install` |

> **⚠️ Security.** By default the gateway + dashboard bind to **`127.0.0.1` (this Mac only)**.
> They have **no authentication**, and the dashboard can run **any shell command** via an agent.
> Only set `bind_host: 0.0.0.0` on a network you fully trust (home LAN / VPN) — on an untrusted
> network, anyone who can reach the port can run commands on your Mac.

> **Housekeeping.** App builds run as plain local servers (no Docker bloat). If free space ever
> dips, `law reclaim` trims the Docker VM + caches (never touches models); `law reclaim --media 7`
> also drops generated images/audio/video older than 7 days.

### Running it again / updating
```bash
./install.sh
```
A healthy machine prints `everything already up to date — nothing changed`.
After unpacking a new version, run it once: it updates the `law` command and restarts
the services that changed (jobs submitted before that restart may fail with
`unknown job kind` — just run them again). Open a **new terminal** after the first install
so `law` is on your PATH (or run `source ~/.zprofile`).

### `install.sh` options

| Command | What it does |
|---|---|
| `./install.sh` | Check everything; install / repair / remove as needed |
| `./install.sh --dry-run` | List every change it *would* make (`+` add, `~` update, `-` remove) and change nothing |
| `./install.sh --upgrade` | Also re-resolve the Python packages to the newest allowed versions and pull newer Docker images |
| `./install.sh --keep-v1` | Don't retire the old v1 stack (`mlx-setup.sh`) during this run |
| `./install.sh --purge` | Remove v2 (see [Uninstall](#uninstall)) |
| `./install.sh --help` | Show these options |

Options can be combined, e.g. `./install.sh --upgrade --dry-run`.

**Environment variables** (advanced): `LAW_HOME` (default `~/.law`), `LAW_REPO`,
`LAW_BRANCH` (default `v5`), `HF_HOME` / `HF_HUB_CACHE` (where models are stored).

---

## Quick start

```bash
law status                              # is everything healthy?
law run "explain a mutex in 3 sentences"           # chat (no tools)
law agent "build tic tac toe with a 3x3 grid for the web, run it on local docker" \
          -C ~/MLX-AI/source/tictactoe            # an agent that actually builds it
law jobs                                # what's queued / running / waiting / done
law rollback ~/MLX-AI/source/tictactoe  # undo everything the agent changed
law configure                           # optional: Telegram bot, Hugging Face token
```

Open the chat UI in a browser: **http://localhost:3001** (Open WebUI) and pick the
`general` model. From another device on your network use `http://<mac-ip>:3001`
(`law status` prints the Mac's LAN IP). Forgot the Open WebUI password?
`law webui-reset-password`.

The first job for each model takes longer: the model has to load into memory
(~5 s for 16 GB, ~10 s for 42 GB on an M5 Pro). In Open WebUI you'll see this as
`[law] Loading qwen38-27b (~16 GB)…` in the collapsible thinking area. After that the
model stays loaded until a different model is needed or 15 minutes pass with no jobs.

---

## Agents

`law run` is a plain chat: the model answers in text. `law agent` gives a persona **tools**
and lets it work in a project folder until the task is done.

```bash
law agent "create hello.py that prints today's date, run it, and show the output" \
          -C ~/MLX-AI/source/agent-test
law agent -p coder "the tests in tests/test_api.py fail — find out why and fix it" -C .
law agent -p researcher "compare the 3 best open-source TTS models for Mac, with links" \
          -C ~/MLX-AI/source/research
law agent -p orchestrator "have the coder write add.py with a test, then have qa check it" \
          -C ~/MLX-AI/source/agent-test
```

### What an agent run does
1. **Checks the folder.** `-C` picks the project folder (default: the current folder). It
   is created if missing. Your home folder itself and `/` are refused
   — an agent works inside *one* project.
2. **Takes a snapshot** of the folder (undo point) and prints how to undo:
   `[snapshot 3f2a… — undo everything this agent does with: law rollback … --to 3f2a…]`.
3. **Translates the request** (top-level runs only): your casual wording becomes a task spec
   with goal, deliverables, assumptions, steps, "definition of done" and verify commands.
   It's saved to law's store (keyed by project + job id) and the assumptions are shown, e.g.
   `[spec] Create and run hello.py that prints today's date → @coder`. Your original request
   is kept in the spec and wins if anything conflicts. It never waits for approval. If the
   model's first answer isn't a valid spec it is asked once more; if that fails too the
   request is used as written. `--no-translate` skips this.
4. **Works in steps.** Each step the model either calls tools or gives its final report.
   You see what it says, every tool call and a one-line result:
   ```
   [step 1] I'll create hello.py and run it.
   [step 1] write_file {"path": "hello.py", "content": "from datetime import date…"}
   [step 1]   ↳ 0.0s: created /Users/you/MLX-AI/source/agent-test/hello.py (62 characters)
   [step 2] run_shell {"command": "python3 hello.py"}
   [step 2]   ↳ 0.1s: [exit code 0, 0.1s]
   ```
5. **Finishes** with its report, then a summary line
   `[3 step(s) · 2 tool call(s) · project …]` and the undo command.

Everything runs through the queue as one job (`law jobs` shows `KIND agent`), so it waits
its turn and never runs at the same time as another job.

### How law keeps local models on task
Local models served by mlx-openai-server don't always come back with clean tool calls. On
a real M5 Pro run, `qwen38-27b` replied *"Simple task. Create the file, run it, verify.
&lt;/think&gt;"* and stopped — 80 tokens, no tool call, nothing created. law now handles this:

| Problem | What law does |
|---|---|
| Tool calls lost while streaming | Agent turns are fetched as **one whole reply** (`agent.stream: false`); the model server parses tool calls more reliably that way |
| Thinking leaks into the answer (`…</think>`) | Everything up to `</think>` is moved out of the answer |
| The model writes a tool call as **text** (`<tool_call>{…}</tool_call>`, Qwen3-Coder `<function=…>` XML, or a JSON block) | It is recognised and run anyway; you see `recovered 1 tool call(s) the model wrote as text` |
| The model "finishes" without ever calling a tool | It is told that nothing happened on the Mac and asked to do the work: up to `agent.nudges` (2) times for an empty reply, **once** for a real answer (it may simply be the right answer to a question). If it still does nothing, the report ends with **"this agent did not use any tool, so nothing was changed"** |
| An empty reply in the middle of the work | It is asked to continue |
| You need to see what the model really sent | Every raw reply is saved in law's store (runs, per project + job id: `step-<n>`, `translate-<n>`) |

The same whole-reply + text-call recovery is used for apps that send tools through the
gateway (OpenCode, Claude Code) — see [`tools_whole_reply`](#configuration).

### Tools

| Tool | What it does |
|---|---|
| `run_shell` | Any bash command in the project folder (git, docker, brew, npm, pytest, curl…). Output and exit code are returned; time limit `shell_timeout_s` (30 min) |
| `read_file` | Read a text file (optionally a line range) |
| `write_file` | Create or overwrite a file (folders are created) |
| `edit_file` | Replace an exact piece of text in a file — safer than rewriting it |
| `list_dir` | List a folder (2 levels by default; skips `.git`, `node_modules`, …) |
| `web_search` | Search the web through your private SearXNG |
| `web_fetch` | Download a page as plain text |
| `remember` | Save a durable fact to the project's memory in law's store; future runs see it (`law agent --show-memory`) |
| `delegate` | Hand a sub-task to another persona (see below) |

Relative paths resolve inside the project folder; absolute paths work anywhere — **agents
have full access to your Mac**, as designed. There are no approval prompts. Tool failures
come back to the model as `ERROR: …` so it can adapt; long output is shortened (start and
end kept).

**Hard-block list** — the only commands refused, because no snapshot could undo them:
deleting `/` or your home folder (`rm -rf /`, `rm -rf ~`), erasing/partitioning disks
(`diskutil erase…`), formatting (`mkfs`), writing raw data to a disk device (`dd of=/dev/disk…`),
and fork bombs. The agent gets `REFUSED: …` and has to find a narrower way.

### Personas for agents
Each persona gets its own short role on top of shared rules (plan → do with tools → verify →
fix → report). `general` (default) does everyday tasks; `coder` reads code first, writes
tests and runs them; `writer` saves documents (and uses `pandoc` for .docx/.pdf);
`researcher` uses web search and cites URLs; `qa` checks work critically; `orchestrator`
splits big jobs and delegates. Pick one with `-p`. For code, `-p coder` is usually the best
choice — it is also ~3× faster than `general` on an M5 Pro.

### Delegation (sub-jobs)
An agent can call `delegate(persona, task)`. That creates a **sub-job** (another persona,
same project folder, higher priority) and parks the parent as `waiting`. Because there is
only one worker, the sub-job runs next — loading its own model if needed — and when every
sub-job has ended the parent continues and receives each sub-job's final report (or its
error) as the tool result. Briefs are saved to law's store (handoffs, per project + job id).

- Sub-jobs don't get the translator (the brief is already precise).
- Depth is limited by `max_depth` (default 2: an agent can delegate, its sub-job can
  delegate once more, and that's it).
- `law cancel <parent>` cancels its sub-jobs too; a cancelled or failed sub-job wakes the
  parent with the reason, so it never waits forever.

### Resume
The whole conversation is saved after every step. So:
- a job interrupted by a restart or crash continues from the last finished step;
- a **failed**, **cancelled** (including Ctrl+C) or **max-steps-reached** job can be continued:
  ```bash
  law agent --resume 42          # or /resume 42 in Telegram
  ```
  It goes back in the queue with a fresh retry budget and picks up where it stopped (a
  cancelled sub-job is reported to it as cancelled, not re-run).
- `law agent --resume <id>` on a job that is still queued/running/waiting just follows it.

### Project memory
Two things give agents per-project context:
- `<project>/.local-agent/AGENT.md` — **your** instructions for agents in this project
  (tech stack, ports, rules). It's the only file law keeps in your project: optional, you
  create it (`law init-project`), and it's added to every agent's prompt (an untouched
  template is ignored).
- **memory** — facts agents saved with `remember`, kept in law's central store (not a file).

`law agent --show-memory -C <folder>` prints the instructions and the saved memory.

### Limits worth knowing
- Local models are capable but smaller than hosted frontier models: give clear goals,
  let the translator add structure, and prefer `coder` for code.
- `max_steps` (40) stops runaway loops; the result tells you how to `--resume`.
- Very long conversations: older tool output is shortened automatically
  (`context_chars`), but a single huge prompt can still hit the
  [300-second limit](#long-prompts-and-the-300-second-limit).

---

## Telegram

Send tasks from your phone. The bot uses the same queue and agents as the CLI and reports
progress while the job runs.

### Set it up (once)
1. In Telegram, open **@BotFather**, send `/newbot`, pick a name, and copy the **token** it gives you.
2. On the Mac:
   ```bash
   law configure
   ```
   - paste the token (law checks it with Telegram and shows `bot @yourbot`);
   - press Enter at "Your Telegram user id" — law asks you to send your bot any message,
     then finds your id automatically (or type it if you know it);
   - optionally add a Hugging Face token.
3. The Telegram service (`com.law.telegram`) starts by itself. Open your bot and send `/help`.

Without questions: `law configure --telegram-token 123:ABC --telegram-user 123456789`.
Check it any time with `law telegram` (status) or `law telegram test` (sends you a message).
Turn it off with `law configure --telegram-off` — the service is removed.

v1 users: your old `TELEGRAM_BOT_TOKEN` / `TELEGRAM_USER_ID` were copied into `~/.law/.env`
during the v1 migration, so the bot starts on the next `./install.sh` without any setup.

### What you can send

| Message | What happens |
|---|---|
| any text | **Agent task** (persona `general` by default) in the chat's project folder |
| `/ask <question>` | Quick chat answer, no tools |
| `/plan <task>` | Only translates the task into a spec and shows it — nothing runs. Then `/go` runs that exact spec, `/raw` runs your words as written |
| `/raw <task>` | Agent task exactly as written (no translator) |
| `/status` | Worker, loaded model, queue, free memory and pressure, your persona and project |
| `/jobs` | Last 8 jobs |
| `/cancel [id]` | Cancel a job and its sub-jobs (default: your last job) |
| `/resume [id]` | Continue a failed / cancelled / stopped agent job (default: your last job) |
| `/undo [id]` | Roll the project back to the snapshot taken before that agent job (default: your last job) |
| `/persona [name]` | Show the personas / change the one used for plain messages (e.g. `/persona coder`) |
| `/project [folder]` | Show / change the project folder. A name means `~/MLX-AI/source/<name>`; `~/…` and `/…` paths work too. Default `~/MLX-AI/source/telegram` |
| `/help` | The list above |

### What you see
Each job gets **one progress message** that is edited every few seconds:
```
job 42 · agent · @general → qwen38-27b
📁 /Users/you/MLX-AI/source/telegram
⚙️ running · 37s

📸 snapshot 3f2a9c1b7e01 (/undo rolls back to it)
📝 spec: Create and run hello.py → @coder
qwen38-27b ready in 5.1s
[1] 🔧 write_file {"path": "hello.py", …}
[1]    ↳ created /Users/you/MLX-AI/source/telegram/hello.py (62 characters)
```
While the agent is working the chat shows **"… is typing"**, and the progress message
streams the model's **live thinking** (🧠) and then its live answer (💬) token by token — the
same reasoning you'd see in a web chat with thinking on — above the tool-call lines.
When the job ends the message turns into `✅ done in 52s · 3 steps · 2 tool calls`
(or `❌ failed` with `/resume <id>`, or `⏹ cancelled`), and the agent's report arrives as a
new message (split into parts if it is long), followed by an `/undo` hint.
Messages are plain text, so code and file names always display correctly.

### Security
Anyone can find and message a Telegram bot, and agents have full access to the Mac. So the
bot **only obeys the user id(s) in `TELEGRAM_USER_ID`** (several: comma-separated). A
stranger gets a single "this bot is private" reply (which shows their id) and is ignored
afterwards. The token and user id live only in `~/.law/.env` (chmod 600) — never in the
service file or the logs.

### Reliability
- Long polling: the bot connects out to Telegram; no port is opened on your Mac.
- Messages sent while the bot was off are processed when it comes back; on the very first
  start, older messages are skipped.
- The update position, per-chat settings (persona, project) and the jobs being followed
  are saved in `~/.law/state/telegram.json`, so a restart neither runs a message twice nor
  loses track of a running job.
- Network down → it retries with growing pauses (up to 1 minute). Telegram rate limits are
  respected. A wrong token is reported in `law logs telegram`.
- The watchdog restarts the bot if it hasn't polled Telegram for 5 minutes
  (`telegram.stale_s`).
- **Sleep-aware.** When the Mac sleeps, every process is frozen while the clock keeps
  moving, so on wake the bot *looks* like it hasn't polled for a long time. The watchdog
  detects the time jump (a gap between its own checks larger than usual) and holds off
  restarts for `watchdog.wake_grace_s` (120 s) so the bot simply resumes polling instead of
  being needlessly restarted. (Before this, a laptop that slept overnight showed a stream of
  "the Telegram bot has not polled for 3340s" restarts in the morning.)

---

## OpenCode

[OpenCode](https://opencode.ai) is a terminal coding agent. law points it at the gateway,
so it uses your local models through the queue.

```bash
law setup-opencode --install      # installs OpenCode with Homebrew if missing, then configures it
cd ~/MLX-AI/source/myapp && opencode
```

`law setup-opencode` adds a provider called **`law`** to `~/.config/opencode/opencode.json`:
one model per persona (`law/coder`, `law/general`, `law/writer`, …), each with the context
limit from the registry so OpenCode compacts the conversation before the model's limit.
`law/coder` becomes OpenCode's model **only if you haven't chosen one**. Your other settings
and providers are kept; the first change saves a copy as `opencode.json.law-backup`.

After that, `./install.sh` keeps the provider in sync whenever models or personas change
(only if OpenCode is installed or `~/.config/opencode` exists; turn off with
`runtime.opencode.manage: false`).

If you keep your OpenCode settings in **`opencode.jsonc`** (with comments), law doesn't edit
it — run `law setup-opencode --print` and paste the block in yourself.

Notes:
- OpenCode sends tools with every request. These requests are answered as one whole reply
  and tool calls the model wrote as text are recovered (`tools_whole_reply`), so the
  answer appears all at once instead of word by word.
- The OpenCode config format above (`provider` / `npm` / `options.baseURL`) is the one in
  OpenCode's current provider docs. If a future OpenCode version changes it, use
  `law setup-opencode --print` as a starting point.
- Like every other client, OpenCode waits its turn in the queue (`law jobs` shows its
  requests as source `api`).

---

## All commands

Run `law --help` or `law <command> --help` at any time.

### Overview

| Command | Purpose |
|---|---|
| `law install` | Same as `./install.sh` minus the bootstrap (needs Homebrew + `law` already installed) |
| `law status` / `law doctor` | Read-only health report |
| `law models` | List models and personas |
| `law pull` | Download optional models |
| `law add-model` | Add a Hugging Face model to your models (checks size + RAM limit) |
| `law remove-model` | Remove one of your models (or your override of a built-in) |
| `law agent` | Give an agent a task with tools in a project folder; resume; show memory |
| `law run` | Send a prompt through the queue and stream the answer (chat, no tools) |
| `law jobs` | List jobs and the worker state |
| `law cancel` | Cancel a queued, running or waiting job (and its sub-jobs) |
| `law logs` | Show worker / gateway / watchdog / telegram / model logs |
| `law probe` | Measure a model: load time, speed, safe prompt size |
| `law snapshot` | Save an undo point for a project folder |
| `law snapshots` | List a folder's undo points |
| `law rollback` | Restore a folder to an undo point (itself undoable) |
| `law configure` | Set secrets: Telegram bot token + user id, Hugging Face token |
| `law telegram` | Telegram bot status / test message / run in the foreground |
| `law setup-opencode` | Point OpenCode at the gateway (and install it) |
| `law start` / `stop` / `restart` | Control the background services |
| `law webui-reset-password` | List Open WebUI accounts and set a new password |
| `law init-project` | Create the optional `.local-agent/AGENT.md` for project instructions |
| `law purge` | Remove v2 state (used by `./install.sh --purge`) |
| `law --version` | Print the version |

### `law install`
```
law install [--dry-run] [--upgrade] [--keep-v1]
```
Runs the install/repair/prune steps. Use `./install.sh` instead on a fresh Mac or after
downloading a new version (it also refreshes the `law` command itself).

### `law status` (alias `law doctor`)
```
law status [--models]
```
Read-only; never changes anything. Shows:
- **Machine** — chip, RAM, macOS version, free memory and macOS memory pressure
  (`normal` / `warn` / `critical`)
- **Ports** — which services are up, and whether each is reachable from your LAN.
  `inference :8000` shows `local only (by design — use the gateway)`: the model server
  is deliberately reachable only through the queue.
- **Models** — downloaded or not, context length, RAM-limit warnings, `yours` /
  `your override` markers, probe results (`probe: ~18 tok/s, proven ctx 49152`), and
  `← context too high` if your setting is at or above a size that failed
- **Tools** — every Homebrew package the installer manages
- **Services** — worker (idle / running, loaded model, current job and when it last made
  progress, queue length, agents waiting for sub-jobs), gateway URLs (OpenAI and
  Anthropic), watchdog, Telegram bot (running, last poll — or "not configured"), OpenCode
  provider state, and the watchdog's last 3 actions
- **v1** — whether the old stack is still present

`--models` also asks Hugging Face whether every model repo still exists
(`hf:ok`, `hf:gated`, `hf:NOT FOUND`).

Exit code: `0` healthy, `1` problems found (useful in scripts). An unconfigured Telegram bot
is not a problem; a configured one that isn't running is.

### `law models`
```
law models [-v]
```
Lists every model: id, tier (`core` / `heavy` / `optional` / `user`), size, kind,
download policy (`auto` / `manual`), context length, state (`downloaded`, `-`,
`TOO BIG`), label, and `(yours)` / `(your override)`. Then lists personas and the model
each one uses. `-v` adds repo, license, verification flag and parsers.

### `law pull`
```
law pull <model-id> [<model-id> ...] [--force]
```
Downloads models by id, e.g. `law pull deepseek-r1-70b`. Use it for `manual` models
(the installer only downloads `auto` ones). Safe to interrupt; re-running resumes.
`--force` re-checks the files even if the model looks downloaded (repairs a damaged copy).
Gated models need `HF_TOKEN` (`law configure`).

### `law add-model`
```
law add-model <org/repo> [--id ID] [--kind lm|vlm|embed|tts|stt] [--context N]
              [--tool-parser P] [--reasoning-parser P] [--label TEXT]
              [--persona NAME] [--auto] [--pull] [--force]
```
See [Adding and removing your own models](#adding-and-removing-your-own-models).

| Option | Meaning |
|---|---|
| `<org/repo>` | Hugging Face repo, e.g. `mlx-community/Qwen3-14B-4bit` |
| `--id` | Short id (default: from the repo name, e.g. `qwen3-14b`) |
| `--kind` | Model type (default: guessed from the repo) |
| `--context` | Context length (default 32768 — measure it with `law probe`) |
| `--tool-parser` / `--reasoning-parser` | Override the guessed parsers |
| `--label` | Description shown in `law models` |
| `--persona` | Point a persona at the new model, e.g. `--persona general` |
| `--auto` | Download policy `auto` (the installer keeps it downloaded) instead of `manual` |
| `--pull` | Download it right away |
| `--force` | Replace your existing entry with the same id, or register a model over the RAM limit |

### `law remove-model`
```
law remove-model <id> [--delete-files]
```
Removes one of **your** models, or your override of a built-in model (restoring its
built-in settings). Refuses while a persona in your models file still points at it.
`--delete-files` moves its downloaded files to the trash now (only if `law` downloaded
them); otherwise that happens on the next `./install.sh`. Built-in models can't be
removed — the command tells you how to set them to `download: manual` instead.

### `law agent`
```
law agent [task ...] [-C DIR] [-p PERSONA] [--translate | --no-translate] [--max-steps N] [-d]
law agent --resume JOB
law agent --show-memory [-C DIR]
```
See [Agents](#agents).

| Option | Meaning |
|---|---|
| `task ...` | What to do, in plain words. Quotes optional. If omitted, read from stdin |
| `-C, --dir` | Project folder the agent works in (default: current folder; created if missing) |
| `-p, --persona` | Persona (default `general`; e.g. `coder`, `writer`, `researcher`, `qa`, `orchestrator`) |
| `--translate` / `--no-translate` | Turn the request into a task spec first, or not (default: `runtime.agent.translate` = on) |
| `--max-steps` | Model turns before it stops (default `runtime.agent.max_steps` = 40) |
| `-d, --detach` | Queue it and return immediately |
| `--resume JOB` | Continue a failed / cancelled / max-steps agent job from its saved conversation, or follow one that is still active |
| `--show-memory` | Print the project's `AGENT.md` and memory, then exit |

While it runs you see the snapshot line, the spec, model loading, what the model says at each
step, its tool calls and their results, notes such as recovered tool calls or nudges, then its
final report, `[N step(s) · M tool call(s) · project …]` and the undo command.
**Ctrl+C cancels the job (and its sub-jobs)**; `law agent --resume <id>` continues it.
Exit code `0` = done, `1` = failed/cancelled, `2` = bad arguments, `130` = Ctrl+C.

### `law run`
```
law run [-p PERSONA | -m MODEL] [-s SYSTEM] [-d] [prompt ...]
```
Queues a prompt and streams the answer to the terminal (plain chat — no tools; use
`law agent` for work).

| Option | Meaning |
|---|---|
| `prompt ...` | The prompt. Quotes are optional. If omitted, read from stdin |
| `-p, --persona` | Persona to use (default `general`). See `law models` |
| `-m, --model` | Use a specific model id instead of a persona |
| `-s, --system` | System prompt |
| `-d, --detach` | Queue it and return immediately (follow with `law jobs`) |

While it runs you see grey status lines — `[queued, position 2]`,
`[waiting for memory: 12 GB free, …]`, `[loading qwen3-coder-next (~42 GB)…]`,
`[thinking] …` (the model's reasoning), `[tool call] …`, `[warning] …` — then the answer
and `[done in 12s · job 7]`.
**Ctrl+C cancels the job.** Exit code `0` = done, `1` = failed/cancelled, `130` = Ctrl+C.

Examples:
```bash
law run "what is 17 * 23?"
law run -p writer "draft a polite reminder email about the invoice"
law run -m qwen36-35b -s "Answer in Bahasa Melayu" "what is a mutex?"
cat error.log | law run -p coder "explain this error and how to fix it"
law run -d -p reasoner "plan a 3-day trip to Penang"      # don't wait
```

### `law jobs`
```
law jobs [-n N]
```
First line: worker state, loaded model, current job, last heartbeat, queue length.
Then the latest `N` jobs (default 20): id, status, attempt count, kind (`chat` / `probe` /
`agent`), model, source (`cli` / `api` / `anthropic` / `agent` / `telegram`), age, and the
prompt (or the error for failed/cancelled jobs). Sub-jobs show `↳ sub-job of <parent> (@persona)`.

Job statuses: `queued` → `running` → `done` | `failed` | `cancelled`; agents that delegated
are `waiting` until their sub-jobs end. A job can go from `running` back to `queued` when it
is retried or the worker restarts.

### `law cancel`
```
law cancel <job-id>
```
Cancels a queued, running or waiting job, **and all its sub-jobs**. A running job stops within
about a second (a running shell command is killed). Cancelling a sub-job wakes its parent,
which is told the sub-task was cancelled. Finished jobs can't be cancelled.

### `law logs`
```
law logs [worker | gateway | watchdog | telegram | <model-id>] [-n N] [-f]
```
| Example | Shows |
|---|---|
| `law logs` | Worker log (default) — includes agent job starts, waits and errors |
| `law logs gateway` | Gateway (API) log |
| `law logs watchdog` | Every check the watchdog acted on |
| `law logs telegram` | Telegram bot log (connection problems, ignored strangers, wrong token) |
| `law logs qwen38-27b` | That model server's log — check this when a model fails |
| `law logs worker -n 200` | Last 200 lines |
| `law logs worker -f` | Follow live (Ctrl+C to stop) |

If the name doesn't exist, it lists the available logs. For what an agent's model actually
replied, look in law's store (runs) — `law agent --show-memory` summarises a project's saved state.

### `law probe`
```
law probe <model-id> [--sizes 4k,16k,32k] [-d]
law probe --show [<model-id> ...]
```
Measures a chat model on this Mac — see [Measuring models](#measuring-models-law-probe).

| Option | Meaning |
|---|---|
| `<model-id>` | The model to probe (`law models` lists ids). Only chat models (`lm`, `vlm`) |
| `--sizes` | Prompt sizes to try, e.g. `4k,16k,32768`. Default: `runtime.probe.sizes` (4K → 128K) |
| `-d, --detach` | Queue it and return immediately |
| `--show` | Print saved results (all models, or the ones you name) instead of probing |

It runs as a normal queue job: it waits its turn, `law cancel` stops it, Ctrl+C cancels it.

### `law snapshot` / `law snapshots` / `law rollback`
```
law snapshot  [folder] [-m MESSAGE] [--system]
law snapshots [folder] [-n N]
law rollback  [folder] [--to ID]
```
See [Undo](#undo-snapshots-and-rollback). `folder` defaults to the current folder.

| Option | Meaning |
|---|---|
| `-m, --message` | What the snapshot is for (shown in `law snapshots`) |
| `--system` | Also take a macOS APFS local snapshot of the whole disk (Time Machine) |
| `-n` | How many snapshots to list (default 30) |
| `--to` | Snapshot id to restore (default: the latest snapshot) |

### `law configure`
```
law configure                                   # interactive
law configure [--telegram-token T] [--telegram-user ID[,ID]] [--hf-token X] [--telegram-off] [--no-restart]
```
Sets the secrets in `~/.law/.env` (chmod 600) and then starts, restarts or removes the
Telegram service to match.

| Option | Meaning |
|---|---|
| (none, in a terminal) | Asks for the bot token (checked with Telegram), your user id (found automatically after you message the bot) and an optional Hugging Face token. Enter keeps the current value, `-` removes it. Secrets are typed hidden |
| `--telegram-token` | Bot token from @BotFather — checked with Telegram before it is saved |
| `--telegram-user` | Your Telegram user id; several people: `111,222` |
| `--hf-token` | Hugging Face token (only needed for gated models) |
| `--telegram-off` | Remove the Telegram token and user id; the bot service is removed |
| `--no-restart` | Only save; apply later with `./install.sh` |

### `law telegram`
```
law telegram [status | test | run]
```
| Action | Effect |
|---|---|
| `status` (default) | Bot name (checked live), allowed user ids, service running or not, last poll, jobs being followed |
| `test` | Sends a test message to every allowed user (open the bot and press **Start** first) |
| `run` | Runs the bot in this terminal, for debugging (the service must be stopped — only one bot may poll) |

### `law setup-opencode`
```
law setup-opencode [--install] [--print]
```
See [OpenCode](#opencode). `--install` installs OpenCode with Homebrew if it is missing;
`--print` only prints the provider block (for `opencode.jsonc` users).

### `law start` / `law stop` / `law restart`
| Command | Effect |
|---|---|
| `law start` | Start the worker, gateway, watchdog (and the Telegram bot if configured). They also start automatically at login |
| `law stop` | Stop them all and unload the model. **Queued jobs are kept** and run after `law start`. The watchdog is stopped first so it doesn't restart the others |
| `law restart` | Restart them all (a running job goes back into the queue; agents continue from their last finished step) |

### `law webui-reset-password`
```
law webui-reset-password [--email you@example.com]
```
Lists the Open WebUI accounts, asks which one (picks it automatically if there is only
one), asks for the new password twice (hidden), and sets it. Your chats and settings are
kept. The password is handed to the container privately — it never appears on a command
line or in shell history. If there are no accounts yet, it tells you to sign up (the first
account becomes the admin).

### `law init-project`
```
law init-project [folder]
```
Creates the optional `<folder>/.local-agent/AGENT.md` so you can give agents project
instructions. Creates the folder if it doesn't exist; never overwrites an existing
AGENT.md. Running `law agent` just creates the folder — it does **not** need this.

```
<project>/.local-agent/AGENT.md   ← the ONLY file law keeps in your project (optional, you edit it)
```

Everything else an agent produces — task specs, delegation briefs, raw model replies and
saved memory — lives in law's central store (`~/.law/state/agent.db`), keyed by the
project path, so your project folders stay clean. Undo snapshots live in
`~/.law/state/undo/`. View a project's instructions, memory and spec count with
`law agent --show-memory -C <folder>`.

### `law purge`
```
law purge --yes
```
Low-level removal used by `./install.sh --purge`. Prefer `./install.sh --purge`.

### Old v1 commands
`law` accepts the old `mlx-setup.sh` flags:

| v1 flag | Now |
|---|---|
| `--bootstrap`, `--wizard`, `--pull-models`, `--pull-heavy`, `--pull-vision` | `law install` |
| `--status` | `law status` |
| `--list-models` | `law models` |
| `--download-model <id>` | `law pull <id>` |
| `--probe-model <id>` | `law probe <id>` |
| `--add-model <repo>` | `law add-model <repo>` |
| `--remove-model <id>` | `law remove-model <id>` |
| `--start`, `--stop`, `--restart` | `law start` / `stop` / `restart` |
| `--init-project` | `law init-project` |
| `--agent "<task>"` | `law agent "<task>"` |
| `--resume <id>`, `--show-memory` | `law agent --resume <id>`, `law agent --show-memory` |
| `--configure` | `law configure` |
| `--setup-opencode` | `law setup-opencode` |
| `--telegram`, `--telegram-service` | `law telegram` (the bot itself runs as a service once configured) |
| `--image`, `--vision` | Phase 6 |

Flags that aren't available yet print which phase adds them.

---

## Using it from other apps (API)

The **gateway** speaks both the OpenAI and the Anthropic API:

```
OpenAI:     http://localhost:4000/v1          (LAN/VPN: http://<mac-ip>:4000/v1)
Anthropic:  http://localhost:4000             (LAN/VPN: http://<mac-ip>:4000)
```
No API key is checked — apps that require one can use any value (e.g. `local`).
Every request becomes a queue job, so many apps can use it at once safely; they take turns
(also with agents and Telegram).

**Model names (OpenAI API):** a persona (`general`, `coder`, `writer`, …) or a model id
(`qwen38-27b`, …). `default` or an empty name means `general`. Only downloaded models are
listed. For the Anthropic API see the next section.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Worker state + queue length |
| GET | `/v1/models` | Available personas and models |
| POST | `/v1/chat/completions` | OpenAI chat. Supports `stream`, `tools`, `tool_choice`, `temperature`, `top_p`, `max_tokens`, `stop`, `response_format`, `seed`, penalties |
| POST | `/v1/messages` | Anthropic Messages API. Supports `system`, text, images, `tools`, `tool_choice`, tool use / tool results, `stop_sequences`, `temperature`, `top_p`, `max_tokens`, `stream` |
| POST | `/v1/messages/count_tokens` | Anthropic token estimate (~4 characters per token) |
| GET | `/law/jobs` | Last 50 jobs (including agent jobs) |
| GET | `/law/jobs/<id>` | One job |
| GET | `/law/jobs/<id>/events?after=N` | The job's event stream (status, model loading, tokens, agent steps, warnings, done/error) |
| POST | `/law/jobs/<id>/cancel` | Cancel a job (and its sub-jobs) |

**Requests with tools** (OpenCode, Claude Code, your own agents) are answered as **one
whole reply** by default (`runtime.gateway.tools_whole_reply: true`): the model server parses
tool calls more reliably that way, thinking that leaked into the text is removed, and tool
calls the model wrote as text are turned into real tool calls. The reply is still sent as a
normal stream to the client — it just arrives all at once. Requests without tools stream
word by word as before.

**OpenAI streaming** responses carry the job id in the `X-Law-Job` header. While the job
waits in the queue or a model loads, the stream sends **progress lines** as
`reasoning_content` (the "thinking" field), each starting with `[law]`:

```
[law] Waiting in queue, position 2…
[law] Unloading qwen3-coder-next to free memory…
[law] Loading qwen38-27b (~16 GB) — the first answer takes a moment…
[law] qwen38-27b ready in 9.4s.
[law] Still working… (40s)
```
They stop as soon as the model starts answering and are **never** part of the answer
(`content`). Non-streaming responses never include them. Turn them off with
`runtime.gateway.progress: false` — then only invisible keep-alive comments are sent.

**Anthropic streaming** sends standard `ping` events while waiting instead.
**If a streaming client disconnects, its job is cancelled.**

### Examples
```bash
curl http://localhost:4000/v1/models

curl http://localhost:4000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"general","messages":[{"role":"user","content":"hi"}]}'

curl -N http://localhost:4000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"coder","stream":true,"messages":[{"role":"user","content":"fizzbuzz in go"}]}'

curl http://localhost:4000/v1/messages -H 'Content-Type: application/json' -H 'x-api-key: local' \
  -d '{"model":"claude-sonnet-4-5","max_tokens":500,"messages":[{"role":"user","content":"hi"}]}'

curl -X POST http://localhost:4000/law/jobs/7/cancel
```
```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:4000/v1", api_key="local")
r = client.chat.completions.create(model="general",
                                   messages=[{"role": "user", "content": "hi"}])
print(r.choices[0].message.content)
```
```python
import anthropic
client = anthropic.Anthropic(base_url="http://localhost:4000", api_key="local")
r = client.messages.create(model="claude-sonnet-4-5", max_tokens=500,
                           messages=[{"role": "user", "content": "hi"}])
print(r.content[0].text)
```

---

## Claude Code and other Anthropic clients

Point any Anthropic-API client at the gateway. For Claude Code:
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000      # or http://<mac-ip>:4000 from another device
export ANTHROPIC_AUTH_TOKEN=local                    # any value; nothing is checked
claude
```

**Which local model answers?** Anthropic clients ask for names like `claude-sonnet-4-5`
or `claude-haiku-4-5`. The gateway maps them with `runtime.gateway.anthropic_models` in
the registry:

```yaml
anthropic_models:
  haiku: general      # any name containing "haiku" → the general persona (qwen38-27b)
  "*": coder          # everything else → the coder persona (qwen3-coder-next)
```
The first rule whose text appears in the requested name wins; `"*"` catches the rest.
You can also send an exact persona or model id as the model name.

Notes:
- Local models are smaller than Anthropic's hosted ones — expect them to be slower and
  less capable on very large tasks. Keep prompts within the model's proven size
  (see [Long prompts](#long-prompts-and-the-300-second-limit)).
- Claude Code sends tools with every request, so its answers arrive as one whole reply
  (see `tools_whole_reply` above).
- Thinking blocks the client sends back are dropped (local models can't verify them);
  the models' own reasoning is not forwarded to Anthropic clients.
- Requests from Anthropic clients show as source `anthropic` in `law jobs`.

---

## Web UIs and ports

| Port | Service | Reachable from LAN | Notes |
|---|---|---|---|
| 3001 | **Open WebUI** — chat UI | yes | Already connected to the gateway |
| 8888 | **SearXNG** — private web search | yes | Used by the agents' `web_search` tool |
| 9000 | **Portainer** — Docker management | yes | Create an admin user on first visit |
| 4000 | **Gateway** — OpenAI + Anthropic API | yes | See above |
| 8000 | Model server | **no — by design** | Internal (127.0.0.1); always reached through the queue |
| 8800 | **Dashboard** — live command center | yes | Watch and dispatch agents; see [below](#dashboard-command-center) |
| 8081 | Vision server | — | Reserved, Phase 6 |
| 7860 | ACE-Step music | — | Phase 6 |

The Telegram bot opens no port — it connects out to Telegram.

### Dashboard (command center)

Open **`http://<your-mac>:8800`** (or `http://127.0.0.1:8800` locally). It is a live,
read-and-command view of the whole workstation — not a log page:

- **Now — live:** the agent currently working, streaming its *thinking* and its answer token
  by token (the same reasoning you see in a web chat with thinking on), plus every tool call
  and delegation as it happens.
- **Agents & delegation:** a Slack/Jira-style board of the active jobs. Each agent is a lane
  (orchestrator at the top, the specialists it delegated to nested under it) with a live
  progress bar and its latest step.
- **Board:** recent jobs with status; a **cancel** button on anything still running.
- **Command bar:** type a task, pick which agent, optionally a project folder, and
  **Dispatch** — it becomes a normal queue job exactly like `law agent` or a Telegram message.
- **Studio & services:** one-click tiles to Open WebUI (chat), the ACE-Step music studio,
  SearXNG and Portainer, plus the machine's memory and every port's up/down state. (Image and
  video generation are marked *phase 6* — the tiles appear once those pipelines are wired.)

The dashboard runs as its own service (`com.law.dashboard`) and only ever talks to the job
queue, so it can never slow down or break model serving. Like the gateway it has **no
authentication** — use it on a network you trust.

There is **no authentication** on the gateway by design — only use this on networks you
trust (home LAN, your own VPN). Open WebUI has its own local accounts (it stores your chat
history); the first account to sign up becomes the admin. Reset a password with
`law webui-reset-password`.

The web UIs run in **Colima** (a small Linux VM for Docker: 4 CPU, 4 GB RAM by default)
and start automatically at login.

---

## Models and personas

### Default models

| id | Size | Downloaded | Context | Used for |
|---|---|---|---|---|
| `qwen38-27b` | 16 GB | auto | 49152 (measured) | **Default.** General, writing, QA, reasoning |
| `qwen36-35b` | 20 GB | auto | 65536 (not measured yet) | Orchestrator, researcher, business analysis |
| `qwen3-coder-next` | 42 GB | auto | 32768 (measured) | Coding — and the fastest (~60 tok/s on M5 Pro) |
| `qwen3-vl-8b` | 6 GB | auto | 32768 | Vision / OCR (wired in Phase 6) |
| `qwen3-embed-8b` | 5 GB | auto | — | Embeddings for search over your files (Phase 5) |
| `kokoro-82m` | 1 GB | auto | — | Text-to-speech (Phase 6) |
| `deepseek-r1-70b` | 37 GB | manual | 32768 | Optional heavy reasoner — `law pull deepseek-r1-70b` |
| `deepseek-r1-32b` | 18 GB | manual | 32768 | Optional reasoner |
| `qwen3-coder-30b` | 15 GB | manual | 65536 | Optional lighter coder |
| `gemma3-12b-encoder` | 8 GB | manual | — | Text encoder for LTX video (Phase 6) |

`auto` models are downloaded by every install run if missing. `manual` models are only
downloaded by `law pull`, and are kept if already present.

**Measured on an M5 Pro 64 GB:** `qwen38-27b` loads in ~5 s, ~18 tokens/s; 32K-token prompts
read in ~88 s, 48K in ~156 s with 15 GB RAM still free; an ~85K-token prompt hit the model
server's 300-second limit — so its context is **49152**. `qwen3-coder-next` loads in ~11 s,
~60 tokens/s, 32K OK with ~5 GB RAM left; 64K would not fit in memory. Your numbers depend on
your Mac — run `law probe`. (If you added your own `context` override for qwen38-27b earlier,
`law remove-model qwen38-27b` removes it so the default applies.)

**RAM limit:** models with weights over **46 GB** are refused (they wouldn't leave
enough memory for macOS and the model's working memory). Change it in the registry if
you know what you're doing.

### Personas

| Persona | Model | Purpose |
|---|---|---|
| `general` (default) | qwen38-27b | Answers and everyday tasks |
| `orchestrator` | qwen36-35b | Plans big tasks, delegates sub-jobs |
| `researcher` | qwen36-35b | Web research with sources |
| `ba` | qwen36-35b | Business analysis / requirements |
| `coder` | qwen3-coder-next | Writes, tests and fixes code |
| `writer` | qwen38-27b | Long documents |
| `qa` | qwen38-27b | Reviews and checks results |
| `reasoner` | qwen38-27b | Careful step-by-step reasoning |
| `deep-reasoner` | deepseek-r1-70b | Optional heavy reasoner (pull it first) |
| `vision` | qwen3-vl-8b | Images, screenshots, OCR |
| `designer` | qwen3-vl-8b | Design review from images |

With `law run` / the API / OpenCode a persona selects the model. With `law agent` and
Telegram it also selects the agent's role and behaviour (see [Agents](#agents)). Point a
persona at another model with `law add-model … --persona NAME` or in your models file.

---

## Adding and removing your own models

```bash
law add-model mlx-community/Qwen3-14B-4bit             # register it (manual download)
law add-model mlx-community/Qwen3-14B-4bit --pull      # register + download now
law add-model mlx-community/Qwen3-14B-4bit --persona writer --pull
law probe qwen3-14b                                     # how long a prompt is safe?
law run -m qwen3-14b "hi"
law remove-model qwen3-14b --delete-files
./install.sh                                            # restarts services; updates OpenCode's model list
```

`law add-model`:
1. asks Hugging Face whether the repo exists (clear messages for typos, gated repos and
   no internet)
2. adds up the repo's weight files → the size used by the RAM guard and the disk check
3. **refuses repos over the RAM limit** (46 GB by default) unless `--force`
4. guesses the kind (chat / vision / embeddings / speech) and the tool-call and reasoning
   parsers from the repo — every guess can be overridden with a flag
5. warns if the repo isn't tagged as an MLX model (it may not load) or is gated
6. writes the entry to `~/.law/config/models.user.yaml` and validates the result

Your models are marked `(yours)` in `law models` and `yours` in `law status`.
Built-in models can't be removed, but you can change any of their settings with an
override in your models file (shown as `your override`); `law remove-model <id>` on an
override removes it and restores the built-in settings.

---

## How a job runs

1. **Queued** — stored in SQLite (`~/.law/state/queue.db`). Survives restarts, crashes
   and reboots.
2. **Taken by the single worker** (one job at a time; higher priority first — sub-jobs sit
   above their parent — then oldest first).
3. **Model check** — refused if over the RAM limit or not downloaded (no retry; the
   error says what to do).
4. **Model swap if needed** — the current model is stopped, the worker waits until the
   port is free and RAM is actually released (up to `ram_wait_s`), checks memory pressure
   (see [memory guard](#memory-guard)), then starts the new model and waits until it answers.
5. **Work** — a chat streams its answer (a chat with tools gets one whole reply); an agent
   runs its tool loop (one whole model reply per step); a probe measures.
6. **Done**, or:
   - model fails to load / crashes / sends nothing for **10 minutes** → retried once on a
     freshly started model, then `failed` with the reason and the last log lines
   - the prompt is too long for the model server's 300-second reading limit → `failed`
     immediately with that explanation (retrying wouldn't help)
   - an agent delegated → `waiting` until its sub-jobs end, then queued again
   - you cancel → stops within ~1 second (with its sub-jobs)
   - worker stopped, restarted or crashed → the job goes back to the queue and runs
     again (agents from their last finished step) — at most 3 times, then it is failed
     (see [interruptions](#interruptions))

After **15 minutes without jobs** the model is unloaded to give the RAM back.

### Timeouts (all configurable, see [Configuration](#configuration))

| Setting (`runtime.worker`) | Default | Meaning |
|---|---|---|
| `load_timeout_s` | 1800 | Max time for a model to load |
| `first_token_timeout_s` | 1800 | Max wait for the first token (long prompts take time); also the limit for one whole reply |
| `idle_token_timeout_s` | 600 | Max gap between tokens before the job is retried |
| `idle_unload_s` | 900 | Unload the model after this long with no jobs |
| `ram_wait_s` | 120 | How long a model load waits for RAM to be released |
| `max_attempts` | 2 | Attempts per job after errors (1 retry) |
| `max_interruptions` | 3 | Crashes/restarts a job may survive before it is failed |
| `default_max_tokens` | 16384 | Default answer length limit |

Agent limits live in `runtime.agent` (see [Configuration](#configuration)). There is no limit
on total job time — a long answer or a long agent run is fine as long as it keeps making progress.

---

## Reliability: watchdog, memory guard, interruptions

### Watchdog
A background service, `com.law.watchdog`, checks every 15 seconds:

| Check | Action |
|---|---|
| Worker hasn't reported "alive" for 90 s (frozen or crashed) | Restart the worker |
| A job has made no progress for longer than the worker's own timeouts allow (default: the longest of model load, first token and agent shell timeouts + 300 s) | Restart the worker; the job goes back in the queue |
| Gateway `/health` fails 3 checks in a row | Restart the gateway |
| The Telegram bot (when configured) hasn't polled for 5 minutes | Restart the bot |
| macOS memory pressure is **critical** while a job runs | Add a warning to that job (shown in the terminal / chat / Telegram), at most every 5 min |

Rules: the same service is never restarted twice within 2 minutes; a worker you stopped
with `law stop` is left alone. Every action is recorded — `law status` shows the last 3,
`law logs watchdog` shows everything.

How it tells "slow" from "stuck": the worker has a separate heartbeat thread (alive) and
reports progress every time the model sends a token, the loader checks in, a whole reply is
being waited for, or an agent's shell command is still running. Long but working jobs are
never killed.

### Memory guard
Before loading a model the worker compares free RAM with what the model needs
(weights + ~5 % + 2 GB):
- **enough free** → load
- **not enough** → wait up to `ram_wait_s` for macOS to release memory
  (`[law] Waiting for memory…` in the chat)
- **still not enough and macOS memory pressure is critical** → **refuse** with a clear
  message (close apps, run the job again). Loading now would make the Mac swap and hang.
- **still not enough but pressure is normal/warn** → load with a warning (macOS can
  reclaim cached memory)

During an answer, critical pressure produces one warning on the job.
Memory pressure is macOS's own signal (the same one as Activity Monitor's graph);
`law status` (and `/status` in Telegram) shows it.

### Interruptions
If the worker crashes, is restarted (watchdog, `law restart`, reboot) or stopped while a
job runs, the job goes back to the queue — but each time counts as an interruption.
After **3** the job is failed with an explanation instead of being retried forever
(a job that keeps crashing the worker usually means it needs more memory than is free).
Agent jobs keep their saved conversation, so `law agent --resume <id>` can still continue them.

---

## Long prompts and the 300-second limit

The model server (mlx-openai-server 1.8.1) gives up on a request if it hasn't finished
**reading the prompt within 300 seconds**. Reading time grows faster than prompt length,
so on a given Mac every model has a practical maximum prompt size — on an M5 Pro 64 GB,
`qwen38-27b` read 48K tokens in ~156 s but timed out on ~85K tokens.

When this happens, `law` tells you exactly that instead of a bare `500`:
```
job 42 failed: the model server gave up after 300s reading the prompt (~84,927 tokens).
This prompt is too long for this model at this Mac's speed — send less text, split it,
or use a faster model (e.g. the coder). Run `law probe <model>` to see the safe size.
```
Such jobs are not retried (it would fail the same way) and the model stays loaded.

What to do: send less text, split long documents, use the coder model (it reads ~3× faster),
and run `law probe <model>` to know each model's safe size. `law probe` counts a size as
safe only if it was read within `runtime.probe.max_prompt_s` (240 s) — a margin below 300.
Agents shorten old tool output automatically to stay well below this; OpenCode compacts
using the context limits law gives it.

---

## Measuring models: `law probe`

```bash
law probe qwen38-27b                  # default sizes 4K, 16K, 32K, 64K, 128K
law probe qwen36-35b --sizes 16k,32k,48k
law probe --show
```
Runs through the normal queue and:
1. **Cold load** — unloads whatever is loaded and starts the model with a context
   comfortably above the largest size tested; measures load time
2. **Speed** — one short prompt; time to first token and approximate tokens/second
3. **Long context** — for each size hides a secret code inside a long text and asks for it
   back. Records whether it came back, the **time taken (always, even on failure)**, the
   **real prompt length** when the server reports it (later sizes are re-calibrated to it),
   the lowest free RAM and the worst memory pressure. Stops at the first size that isn't safe.
4. **Verdict** — a size is safe when the code came back, ≥ 4 GB RAM stayed free, pressure
   never went critical, and the prompt was read within 240 s. The probe then says what was
   **proven** and only recommends a change the evidence supports:

| Situation | Verdict |
|---|---|
| Your context is at or above a size that **failed** | Lower it to the proven size (prints the lines to add) |
| Your context is above what was **tested** | "Untested" — no change; it tells you which `--sizes` to run |
| Your context is within the proven range | Keep it |
| More was proven than your context | You can raise it |

For each failed size it prints why: model error (with the server's explanation),
prompt read too slowly, code not found, memory pressure critical, or too little RAM free.

Example (real run on an M5 Pro 64 GB):
```
Probe of qwen38-27b
- load: 5.6s
- speed: ~18.0 tokens/s (first token 0.68s)
- ~32768 tokens (real 32,147 tokens): OK · 87.7s · recall yes · min free 24.1 GB · pressure normal
- ~49152 tokens (real 49,751 tokens): OK · 156.2s · recall yes · min free 15.3 GB · pressure normal
- proven safe up to: 49152 · nothing above 49152 tested
- verdict: your context 49152 is within the proven range — keep it
```

**Nothing is changed automatically.** When a change is recommended, the command prints the
lines to add to `~/.law/config/models.user.yaml`; then run `./install.sh`.
Results are saved in `~/.law/state/probes.json`; see them any time with
`law probe --show` or in `law status`. A probe can take from a minute to tens of minutes
for big models and large sizes; use `--sizes` to test fewer sizes.

---

## Undo: snapshots and rollback

```bash
law snapshot ~/MLX-AI/source/myapp -m "before refactor"
# … an agent (or you) changes files …
law rollback ~/MLX-AI/source/myapp                # back to the latest snapshot
law snapshots ~/MLX-AI/source/myapp               # list them
law rollback ~/MLX-AI/source/myapp --to 3f2a9c1b7e01
```

**Every agent run takes a snapshot automatically** before it starts and prints the exact
rollback command (also in the job's final result). From Telegram, `/undo` rolls the project
back to the snapshot of your last agent job (`/undo <id>` for an older one). `/plan` jobs
change nothing, so they take no snapshot.

How it works:
- Each project gets a **private** undo history under `~/.law/state/undo/` (kept out of the
  project so nothing is added to it). It never touches the project's own `.git` (if it has
  one) — your commits and branches are unaffected. An older in-project `.local-agent/undo.git`
  is moved there automatically the first time, preserving its snapshots.
- A snapshot records every file in the folder except `.local-agent/`, `.git/`, files
  matched by the project's `.gitignore`, and files bigger than `runtime.undo.max_file_mb`
  (50 MB — videos, model files; they are listed as skipped).
- `law snapshot` does nothing if no file changed since the last snapshot.
- `law rollback` restores files exactly as they were: changed files are reverted,
  **files created since are removed**, deleted files come back. Ignored and oversized
  files are left alone. If nothing changed since the snapshot it says
  `files already match snapshot … — nothing to undo`.
- **A rollback is itself undoable:** a safety snapshot is taken first and the command
  prints `changed your mind? law rollback … --to <id>` (Telegram shows the same command).
- Undo covers the **project folder**. Things an agent does elsewhere (installing a brew
  package, running a Docker container, editing files outside the folder) are not part of a
  snapshot. `--system` additionally asks macOS for an **APFS local snapshot** of the whole
  disk (Time Machine) as a backstop; restore that from Time Machine / macOS Recovery —
  `law rollback` doesn't restore it. It may need Time Machine to be set up.

---

## What the installer does

Each step compares what *should* exist with what *does* exist. Symbols in the output:
`+` add, `~` update/repair, `-` remove, `·` already OK.

| # | Step | Installs / repairs | Removes |
|---|---|---|---|
| — | Bootstrap (`install.sh`) | Xcode CLT, Homebrew, uv, git, Python 3.12 venv for `law`, `law` command | — |
| 1 | Folders | `~/.law/*`, `~/MLX-AI/{source,outputs}` | — |
| 2 | Secrets file | `~/.law/.env`, permissions 600 | — |
| 3 | v1 settings | Copies v1 secrets, custom models, personas (never overwrites) | — |
| 4 | Retire v1 | Backup first | v1 services, `mlx-agent`, `~/.mlx-ai-workstation` |
| 5 | Homebrew packages | Missing ones | Ones *this installer* added that are no longer listed and that nothing else needs |
| 6 | Python runtime | Venv, locked package set | Packages no longer in the list |
| 7 | Models | Missing or half-downloaded `auto` models | Models it downloaded that are no longer in the registry or your models file (to trash) |
| 8 | Colima | Starts it, fixes CPU/RAM, login auto-start | — (disk is never shrunk) |
| 9 | Web UIs | Missing / stopped / changed containers | Containers no longer needed, old v1 Langfuse |
| 10 | Services | Worker + gateway + watchdog, + Telegram bot when configured; reloads them when code, models or Telegram secrets change | Old `com.law.*` services; the Telegram service when its secrets are removed |
| 11 | OpenCode (optional) | The `law` provider in `~/.config/opencode/opencode.json`, if OpenCode is used | — |
| 12 | Generated files | `~/.law/generated/*` | Leftovers |
| 13 | Trash | — | Items older than 7 days |
| 14 | Version stamp | — | — |

**Safety rules**
- Only one install runs at a time (lock file).
- Interrupted runs continue where they stopped.
- Before downloading, free disk space is checked (trash is emptied early if needed).
- Removed models go to `~/.law/trash` and are deleted after 7 days.
- If a required step fails, the run stops with the reason; full details are in
  `~/.law/logs/install-error.log`. Fix the cause and run `./install.sh` again. The OpenCode
  step is optional: a problem there is only a warning.
- Upgrading keeps your job history: the queue database is upgraded in place.

**Never deleted:** your `.env` secrets · `~/.law/config` · `~/.law/backups` · `~/MLX-AI`
(your projects) · `~/.law/state` (agent store, undo histories, job queue) · `~/ace-step` · Docker
volumes (Open WebUI chats, Portainer data) · models you downloaded yourself · Homebrew
packages that were installed before this installer · your OpenCode settings.

---

## Files and folders

```
~/.law/
├── .env                      secrets (HF_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, SEARXNG_SECRET) — chmod 600
├── config/
│   ├── models.user.yaml      YOUR models + overrides (law add-model / remove-model edit it)
│   └── personas.user.json    your v1 personas (kept for reference)
├── control/.venv/            Python env for the `law` command
├── runtime/                  model-server Python env + requirements.in / .lock
├── state/
│   ├── queue.db              job queue, job history, agent conversations, worker heartbeat, watchdog log
│   ├── agent.db              per-project agent store: task specs, handoffs, raw replies, saved memory
│   ├── undo/                 per-project undo git histories (kept out of your project folders)
│   ├── telegram.json         Telegram: update position, per-chat persona/project, jobs being followed (chmod 600)
│   ├── probes.json           `law probe` results
│   ├── install.json          last result of each install step
│   ├── owned.json            what this installer installed (only these can be removed)
│   └── version
├── generated/                files generated from the registry (don't edit)
├── searxng/settings.yml      generated SearXNG config
├── logs/                     worker.log, gateway.log, watchdog.log, telegram.log, model-<id>.log, install-error.log
├── backups/v1-<time>/        backup of the old v1 config
├── trash/                    removed models, deleted after 7 days
├── run/                      lock files, model.pid, telegram.beat
└── src/                      repo copy (one-liner installs only)

~/MLX-AI/source/              your projects (agent workspace)
  ├── telegram/               default project for tasks sent from Telegram
  └── <project>/.local-agent/AGENT.md   optional project instructions (the only file law keeps here)
~/MLX-AI/outputs/             generated files
~/.cache/huggingface/hub/     downloaded models
~/.config/opencode/opencode.json   OpenCode settings (law manages only the "law" provider)
~/Library/LaunchAgents/com.law.{worker,gateway,watchdog,telegram,colima}.plist   background services
~/.local/bin/law              the `law` command
```

---

## Configuration

### Built-in registry — `law/registry.yaml`
The single source of truth for: ports, models, personas, worker timeouts, watchdog,
gateway behaviour and Anthropic model mapping, probe sizes and limits, undo settings, agent
limits, Telegram and OpenCode settings, Homebrew packages, Python runtime packages, Docker
containers and the Colima VM size. After editing it, run `./install.sh` — services restart
automatically when it changes.

```yaml
runtime:
  worker:      { max_attempts, max_interruptions, idle_unload_s, load_timeout_s,
                 first_token_timeout_s, idle_token_timeout_s, default_max_tokens, ram_wait_s }
  watchdog:    { interval_s: 15, heartbeat_stale_s: 90, stall_s: null,
                 gateway_failures: 3, cooldown_s: 120 }
  gateway:
    progress: true            # "[law] Loading…" lines in the chat's thinking area
    tools_whole_reply: true   # requests WITH tools: one whole reply + recovered text tool calls
    anthropic_models: { haiku: general, "*": coder }
  probe:       { sizes: [4096, 16384, 32768, 65536, 131072], min_free_gb: 4, max_prompt_s: 240 }
  undo:        { max_file_mb: 50 }
  agent:
    max_steps: 40            # model turns per agent job before it stops (resume continues)
    max_depth: 2             # how deep sub-jobs may delegate (0 = no delegation)
    translate: true          # request translator on by default for law agent / Telegram
    shell_timeout_s: 1800    # max time for one shell command
    tool_output_chars: 20000 # longer tool output is shortened (start + end kept)
    max_tokens: 8192         # answer length per model turn
    context_chars: 140000    # older tool output is shortened past this conversation size
    nudges: 2                # empty replies are nudged this often; real answers without tools once
    stream: false            # false = one whole reply per agent turn (more reliable tool calls)
    save_replies: true       # keep raw model replies in law's store (runs) for debugging
  telegram:
    project: telegram        # default project folder for Telegram tasks (~/MLX-AI/source/<name>)
    persona: general         # default persona for plain messages
    poll_s: 50               # Telegram long-poll time
    edit_every_s: 3          # how often the progress message is updated
    progress_lines: 8        # recent steps shown in the progress message
    stale_s: 300             # watchdog restarts the bot after this long without polling
  opencode:
    manage: true             # keep the "law" provider in ~/.config/opencode/opencode.json
    default_model: coder     # set as OpenCode's model only if you haven't chosen one
```

### Your own models — `~/.law/config/models.user.yaml`
Survives updates. Easiest to manage with `law add-model` / `law remove-model`; you can
also edit it by hand. Same fields as the registry. An entry with an existing built-in `id`
overrides only the fields you give (e.g. just `context`).
```yaml
models:
  - id: my-model
    repo: mlx-community/Some-Model-4bit   # Hugging Face repo
    kind: lm                              # lm | vlm | embed | tts | stt
    weights_gb: 12                        # used by the RAM guard and disk check
    context: 32768
    tool_parser: qwen3                    # optional (needed for agents' tool calls)
    reasoning_parser: qwen3               # optional
    download: manual                      # auto = installer downloads it; manual = law pull
    label: "My model"
  - id: qwen36-35b                        # override a built-in model
    context: 32768                        # e.g. after `law probe` recommended it
personas:
  general: { model: my-model }            # point a persona at a different model
```
Then `./install.sh` (or `law pull my-model`) and `law run -m my-model "hi"`.

### Per-project agent instructions — `<project>/.local-agent/AGENT.md`
Plain Markdown added to every agent prompt in that project — tech stack, ports, coding
rules, where things live. It's the only file law keeps in your project; create it with
`law init-project`. Facts agents saved with `remember` live in law's store, not here
(see them with `law agent --show-memory`).

### Secrets — `~/.law/.env`
Set them with `law configure` (or edit the file; it stays chmod 600):
```
HF_TOKEN=hf_...                # only needed for gated Hugging Face repos
TELEGRAM_BOT_TOKEN=...         # from @BotFather
TELEGRAM_USER_ID=...           # your Telegram user id (several: comma-separated)
SEARXNG_SECRET=...             # generated automatically
```
The installer never overwrites existing values; `law configure` changes only what you tell it to.

---

## Moving from v1 (mlx-setup.sh)

On the first run, the installer:
1. copies your secrets (`HF_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`), custom
   models and personas into v2 (the v1 dashboard password is dropped — v2 has no login);
2. backs up the v1 config to `~/.law/backups/v1-<time>/` (its large venv is skipped);
3. stops and removes all `com.mlxstack.*` services, `~/.local/bin/mlx-agent` and
   `~/.mlx-ai-workstation`;
4. recreates Open WebUI / SearXNG / Portainer under v2 — **your Open WebUI chats and
   accounts are kept** (same login as before; `law webui-reset-password` if forgotten);
5. moves v1-only models that v2 doesn't use (e.g. Kimi, Gemma 4) to the trash (7 days).

The v1 `mlx-agent` is replaced by `law agent`; facts in an existing `.local-agent/memory/MEMORY.md` are imported into law's store on first use, and the `.local-agent/` folder is otherwise left alone (only `AGENT.md` is still read)
and upgraded (nothing in them is overwritten). The v1 Telegram bot is replaced by the v2
bot, which starts automatically with your migrated token and user id. The v1 OpenCode
config was backed up; `law setup-opencode` writes the v2 provider. To keep v1 running for
now: `./install.sh --keep-v1`.

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| `law: command not found` | Open a new terminal, or `source ~/.zprofile` |
| Install stops with `✗` | Read the message; details in `~/.law/logs/install-error.log`. Fix, then `./install.sh` again |
| `Another install is running` | Wait for it. If none is running, re-run — stale locks are cleared |
| `needs ~N GB free disk` | Free up disk space, or empty `~/.law/trash` |
| Open WebUI seems slow / silent | Open the thinking area: `[law] …` lines show queue position and model loading. First answer after a model switch takes longer |
| Forgot the Open WebUI password | `law webui-reset-password` |
| Job stays `queued` | `law status` → is the worker running? `law start`, then `law logs worker` |
| Job `failed: unknown job kind 'agent'` | It ran on the old worker before `./install.sh` restarted the services — run it again |
| Agent finished but nothing was created | The report says `did not use any tool` — the model never acted, even after being nudged. The raw replies are in law's store (runs, job `<id>`); try `-p coder` (or `/persona coder`), or a shorter, more explicit task |
| Agent job stays `waiting` | It is waiting for its sub-jobs — `law jobs` shows them (`↳ sub-job of …`). They run next |
| `refusing to use … as the project folder` | Give the agent a project folder: `-C ~/MLX-AI/source/<name>` (Telegram: `/project <name>`) |
| Agent did something wrong | `law rollback <folder>` (printed at the start and end of every run), or `/undo` in Telegram |
| Agent `Stopped after 40 steps` | `law agent --resume <id>` (or `/resume <id>`), or give a smaller / clearer task |
| Agent says `REFUSED: … hard-block list` | It tried something no snapshot could undo (e.g. `rm -rf ~`). It should pick a narrower command; run it yourself if you really mean it |
| Agent's web search fails | `law status` → is SearXNG (`:8888`) up? `./install.sh` repairs it |
| `request translator gave no usable spec` | The model didn't return a valid spec twice; the agent used your request as written. The replies are in law's store (runs, `translate-*`); `--no-translate` / `/raw` skips the step |
| Agent misunderstood the task | Check the spec in law's store (`law agent --show-memory` shows the count); add project rules to `<project>/.local-agent/AGENT.md`; use `/plan` in Telegram to see the spec before it runs; or `--no-translate` with a precise prompt |
| Telegram bot doesn't answer | `law telegram` → token OK? service running? last poll recent? Then `law logs telegram`. The bot must be started once in Telegram (press **Start**) |
| Telegram says "This bot is private" to you | Your user id isn't in `TELEGRAM_USER_ID` — the message shows your id: `law configure --telegram-user <id>` |
| `law logs telegram` shows `409` | Another program polls the same bot (the v1 bot, `law telegram run`, another Mac). Stop the other one |
| `law logs telegram` shows "rejected the bot token" | The token was revoked or mistyped — `law configure` with a fresh token from @BotFather |
| `law telegram test` can't message you | Open the bot in Telegram and press **Start** first (bots can't message you before that) |
| OpenCode doesn't show the `law` models | `law setup-opencode`, then restart OpenCode. If you use `opencode.jsonc`, paste the block from `law setup-opencode --print` |
| OpenCode / Claude Code answers appear all at once | Expected — requests with tools get one whole reply (`tools_whole_reply`) |
| Job `failed: … not downloaded` | `law pull <model-id>` |
| Job `failed: … limit is 46 GB` | Model too big for this machine; use a smaller one |
| Job `failed: … gave up after 300s reading the prompt` | Prompt too long for that model — send less, split it, or use the coder. See [Long prompts](#long-prompts-and-the-300-second-limit) |
| Job `failed: not loading … CRITICAL memory pressure` | Close big apps (browsers, Docker apps, Xcode), then run it again |
| Job `failed: interrupted 3 times` | The worker kept crashing on this job — usually memory. Check `law logs worker`, close apps, try a smaller model or shorter prompt. Agents: `law agent --resume <id>` |
| Job `failed: model server exited …` / `model error: …` | The error includes the last model-server log lines; full log: `law logs <model-id>` |
| `the model stopped sending tokens` | Usually memory pressure. Close big apps, then re-run the job |
| Answer is very slow | `law status` → memory pressure; close large apps |
| `law status` shows `← context too high` | A probe failed at or below your context setting — apply the probe's recommendation |
| `law add-model` says "not tagged as MLX" | Look for an `mlx-community/…` version of the model |
| Claude Code answers with the wrong model | Check `runtime.gateway.anthropic_models`; `law jobs` shows which model each request used |
| `law rollback` removed a file I wanted | Run the `changed your mind? law rollback … --to <id>` line it printed |
| Watchdog keeps restarting a service | `law logs watchdog` and that service's log show why; send both when asking for help |
| Gateway not answering | The watchdog restarts it within ~45 s; otherwise `law restart`, then `law logs gateway` |
| Open WebUI shows no models | `law status` → gateway up? Then refresh Open WebUI |
| A web UI isn't reachable from another device | `law status` shows `NOT reachable from LAN` → check macOS Firewall settings |
| `inference :8000 · local only` | Normal — by design. Use the gateway on port 4000 |
| Colima / Docker errors | `colima status`; `colima start`; then `./install.sh` |
| A model repo shows `hf:NOT FOUND` | It was removed from Hugging Face; remove or replace it (`law remove-model`) |
| Gated model download fails | `law configure --hf-token hf_…` and accept the license on huggingface.co |

When asking for help, include: `law status --models`, `law jobs`, `law logs worker -n 100`,
`law logs watchdog -n 50`, `law logs <model-id> -n 80`, `law logs telegram -n 50` (Telegram
problems), `~/.law/logs/install-error.log` if it exists, and for agents the raw replies in
law's store (`~/.law/state/agent.db`, runs). Never paste your `.env` — it holds your tokens.

---

## Uninstall

```bash
./install.sh --purge
```
**Removes:** worker, gateway, watchdog and Telegram services, Colima login item, v2-managed
Docker containers, the model-server Python env, generated files, state (including job
history, agent conversations, Telegram state and probe results), trash, the `law` command.

**Keeps:** `~/.law/.env`, `~/.law/config`, `~/.law/backups`, downloaded models,
Docker volumes, Homebrew packages, `~/MLX-AI` (your projects), your OpenCode settings
(remove the `law` provider by hand if you like).

To remove everything else by hand afterwards:
```bash
rm -rf ~/.law                               # secrets, your config, backups
rm -rf ~/.cache/huggingface/hub/models--*   # ALL downloaded models
colima delete                               # Docker VM, containers and volumes
```

---

## Development and tests

```
install.sh                bash bootstrap → `law install`
pyproject.toml
law/
├── cli.py                commands
├── runcmds.py            run / jobs / cancel / logs / pull / probe / start / stop / restart /
│                         webui-reset-password / add-model / remove-model / snapshot(s) / rollback
├── agentcmds.py          law agent (new task, --resume, --show-memory)
├── setupcmds.py          law configure / telegram / setup-opencode
├── telegram.py           Telegram bot (Bot API client, commands, progress messages)
├── opencode.py           OpenCode "law" provider (build, merge, write)
├── modelcmds.py          add-model / remove-model logic (Hugging Face lookup, guesses, guards)
├── undo.py               per-project undo history (private git) + APFS local snapshots
├── doctor.py             law status
├── webui.py              Open WebUI account listing + password reset
├── registry.yaml         models, personas, ports, packages, containers, runtime settings
├── registry.py           loads + validates the registry and your overlay
├── engine.py             plan → apply → re-plan step runner
├── manifest.py           ownership records (what may be removed)
├── system.py             every call to brew / colima / docker / uv / launchctl
├── project.py            project folder helpers (optional .local-agent/AGENT.md)
├── paths.py  util.py
├── steps/                install steps: base, v1, brew, python_env, models, docker, services, opencode
├── agent/
│   ├── tools.py          agent tools + hard-block list
│   ├── prompts.py        persona roles, system prompt, nudges, request translator
│   ├── textcalls.py      leaked-thinking split + tool calls written as text
│   └── runner.py         agent job loop: snapshot, translate / plan-only / approved spec, tools,
│                         nudges, delegation, resume
└── runtime/
    ├── db.py             SQLite job queue (+ heartbeat, interruptions, waiting/sub-jobs, agent state, watchdog log)
    ├── store.py          per-project agent store: specs, handoffs, raw replies, memory (SQLite)
    ├── memory.py         free RAM + macOS memory pressure
    ├── modelproc.py      one-model-at-a-time manager, memory guard, server-error explanations
    ├── worker.py         the single worker (+ heartbeat thread; streamed and whole replies; chats with tools)
    ├── gateway.py        OpenAI + Anthropic API (+ progress lines)
    ├── anthropic.py      Anthropic ⇄ OpenAI conversion
    ├── watchdog.py       restarts frozen/stalled worker, gateway and Telegram bot
    └── probe.py          `law probe`
tests/
├── test_phase0.py        installer: fresh Mac, v1 Mac, reruns, repair, pruning, failures
├── test_phase1.py        queue, worker, model swaps, crashes, cancel, gateway, services
├── test_phase2.py        interruptions, DB upgrade, heartbeat, watchdog, memory guard,
│                         chat progress, probe, launcher flags, password reset
├── test_phase3.py        300 s limit handling, probe fixes, Anthropic API, undo,
│                         add-model / remove-model, overrides
├── test_phase4.py        agent tools, hard-block list, agent jobs (snapshot, translator + retry,
│                         delegation, cancel/resume, memory, max steps), leaked thinking,
│                         text tool calls, nudges, CLI
├── test_phase4b.py       Telegram bot (private access, agent/ask/plan→go/raw, progress, cancel,
│                         resume, undo, persona, project, polling), Telegram service + watchdog,
│                         law configure, OpenCode provider, chats with tools via the gateway
├── fakes.py              fake macOS tools
└── fake_mlx_server.py    fake model server (streamed + whole replies; simulates the 300 s
                          limit, agent tool calls, and the ways real local models misbehave)
```
The Telegram tests run against a fake Telegram Bot API server, so no real bot is needed.

Run the tests (works on any OS with git — no Mac tools or models needed):
```bash
pip install -e '.[test]'
cd tests && python -m pytest -q
```
Checks run before every release: `bash -n install.sh`, `python -m py_compile` on all
files, and the full test suite (155 tests).

---

## Roadmap

| Phase | Adds | Status |
|---|---|---|
| 0 | Installer: install / repair / prune, registry, v1 migration, web UIs | ✅ |
| 1 | Job queue, single worker, one-model-at-a-time, gateway, CLI | ✅ |
| 2 | Watchdog, memory guard, interruption limit, chat progress, `law probe`, `law webui-reset-password` | ✅ |
| 3 | Probe fixes + 300 s limit handling, `law add-model` / `remove-model`, undo (snapshots + rollback), Anthropic-compatible API | ✅ |
| 4a | Agents: personas with tools, request translator, sub-jobs (delegation), auto-snapshot before runs, resume, project memory, `law agent` | ✅ |
| 4a.1 | Agents on real local models: whole-reply turns, leaked thinking, text tool calls, nudges, honest "nothing changed" reports, saved raw replies | ✅ |
| 4b | Telegram bot (`/ask /plan /go /raw /status /jobs /cancel /resume /undo /persona /project`), `law configure`, OpenCode via `law setup-opencode`, whole replies + recovered tool calls for apps that send tools | ✅ (this version) |
| 7 | **Dashboard command center** (`:8800`): live agent board with delegation lanes, streaming thinking, dispatch & cancel tasks, service/studio launchers | ✅ (this version) |
| 5 | Search over your files (RAG), longer-term memory | next |
| 6 | Media: image, video, text-to-speech, speech-to-text, music, vision; voice notes and photos in Telegram; image/video studio tiles light up in the dashboard | |
| 8 | Full test harness + migration notes | |
