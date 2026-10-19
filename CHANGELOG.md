# Changelog

## 2.1.0 — 2026-10-18

### Fixed
- **Disk leak (up to ~16 GB per model swap).** mlx-openai-server writes its prompt (KV) cache to a
  temp folder it only deletes on a clean shutdown; law stops models by killing them, so every swap
  or idle-unload left one behind (one Mac collected ~300 GB). The model server now gets
  `TMPDIR=~/.law/run/model-tmp`, which is wiped whenever the model stops; the cache is capped
  (`runtime.worker.prompt_cache_max_gb`, 8 GB); the worker removes old leftovers on start.
- **Dashboard reachable by anyone on the network / by any web page.** It bound to `0.0.0.0`
  regardless of `bind_host`, and its actions (which can start shell-running agents) had no
  cross-site protection. It now honours `bind_host`, and a request guard (trusted networks, Host
  and Origin checks, JSON-only actions) protects the dashboard and the gateway — still no login.
- Gateway CORS no longer `*` (a web page could read your chats); ACE-Step honours `bind_host`.
- Cancelling a job now stops the model writing (it used to keep generating up to `max_tokens`,
  blocking the next job). Non-streamed gateway requests are cancelled when the client hangs up.
- Agent auto-continue and waiting for sub-jobs no longer use up the retry budget.
- Telegram `/daily` runs on wake if the Mac slept through its time (up to 6 h late).
- Vision uploads can't choose a path outside the outputs folder; the worker only deletes a
  project's `.screenshots/` folder if law created it.
- Model downloads no longer empty `~/.law/trash` behind your back when the disk is short.
- `law logs` accepts any service log (`dashboard`, `acestep`, …).
- Tests run in a throw-away HOME (they used to write undo histories into the real `~/.law`) and can
  no longer reach the real launchd — a test of `law configure --telegram-off` used to kill the running
  Telegram bot on every test run, which is why the watchdog kept "restarting telegram".

- **ACE-Step held ~15 GB of memory around the clock** (it started at login and keeps its models
  loaded). It now runs on demand (`law music start|stop|status`, dashboard Music tile) and the worker
  stops it when a model load needs the memory. (On the development Mac the running copy had even
  been started by an old test run from a temp folder.)
- **Thinking leaked into chat answers** (`…</think>answer`) for Qwen3.6 / 3.8: they now use the
  `qwen3_5` reasoning parser, which matches their chat template; Telegram's live view follows suit.
- **The worker could stay down after `./install.sh` reloaded the services** (launchd reported the
  exiting old process as loaded); the reload now waits, and the watchdog revives a worker left
  "stopped" while the other services run.
- Cancelling now stops the model within seconds (it used to wait 30 s for a graceful shutdown).
- Telegram long-polling has a hard deadline and a 10 s connect timeout per address, so a stalled
  network route can't freeze the bot (it was being restarted by the watchdog every few minutes).
- `law probe` turns thinking off for its checks (a thinking model spent the small answer budget
  reasoning and the probe reported a false failure).
- `law jobs` and dashboard cards show the prompt of media jobs.
- Undo snapshots ignore the agents' `.screenshots/` scratch folder.

- Telegram: an agent's final report could be lost on a reset connection — network errors are now
  retried; uploads too. `> quotes` render as quotes.
- Agents' `web_fetch` returned compressed bytes for some sites (python.org) — gzip/deflate is decoded.

### Added
- **Internet first.** Tony (Telegram) web-searches by default and reads the top pages before answering;
  only math, code, writing/translation and small talk are answered from the model's own knowledge.
  Live local time and weather for "time / weather in <place>" (wttr.in). Chat and agent prompts carry
  the current date and time; agents have an "internet first" rule (search, fetch, cite).
- `law image` / `speak` / `video` / `vision` / `transcribe` (the v1 `--image` / `--vision` flags map to them).
- `law unload` and dashboard **⏏ Unload model**.
- `law backup` / `law restore`.
- `law job <id> --md` and the dashboard's **⤓ .md** button — a job as a Markdown report.
- `law probe <id> --apply` saves the measured context as your override.
- `law status --updates` (PyPI check) and a **Disk** section; dashboard **System → Disk** card.
- `law reclaim`: leaked caches, queue pruning, `--trash`, `--dry-run`.
- Dashboard: **plan first** → **Run this plan ▶**, per-model health on Usage, installable on a
  phone (PWA), watched by the watchdog.
- Queue retention (`runtime.queue`), hourly log rotation by the watchdog.
- macOS notification when a long job finishes (`runtime.notify`).
- `machine.max_weights_gb: auto` (RAM − `reserve_gb`).
- SwiftBar / xbar menu-bar plugin (`extras/swiftbar/law.10s.sh`); GitHub Actions CI.

### Changed
- Version is defined once, in `law/__init__.py`.

## 2.0.0 — v2 control plane
Python control plane `law` replacing `mlx-setup.sh`: reconcile installer, job queue + single
worker, OpenAI/Anthropic gateway, agents, Telegram, dashboard, media, RAG.
