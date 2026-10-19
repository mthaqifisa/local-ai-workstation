# CLAUDE.md — LAW v6 standing rules

## Start of every session
1. Read this file, then `PROGRESS.md` and `progress.json`.
2. Continue at the first unchecked task in `PROMPT.md` §3. Never redo completed work unless a check fails.

## Paths
- Code only in `~/SourceCode`. Released v6 code in `~/SourceCode/OpenSourceLocalAI/v6`. Agent-created projects in `~/SourceCode/<project>/`.
- Portable, non-sensitive state lives in v6: `settings/`, `knowledge/`, `trained/` (Git LFS), `templates/`. A fresh Mac must reproduce the setup with `git clone` + `./install.sh`.
- Everything machine-local lives in `~/LAW/` (rendered config, secrets, derived data, models, venvs, Docker, outputs, logs, caches). See PROMPT.md §1 rule 1.
- **Never put secrets in v6.** Secrets only in `~/LAW/state/.env` (0600). gitleaks runs in pre-commit, CI and `law save`.
- Temporary files: `~/LAW/cache/tmp`, never `/tmp`.
- The install script must be safe to run any number of times (PROMPT.md P3 re-run guarantees).

## Progress
- After every task: update `PROGRESS.md` (checklist + dated log line) and `progress.json` (`phase, task, status, files_touched, commands, next_step, timestamp`), then commit on branch `v6`.
- Before any long step, write `next_step` first.

## Ask the user before
- `sudo`, removing brew packages, deleting anything outside `settings/legacy_inventory.yaml`, editing `~/.zprofile`, any P2 destructive step, downloads > 50 GB in one step.

## Quality
- Never self-grade. Pass = deterministic checks + independent judge verdict. Reports are generated from raw results.
- Production-grade only: typed, tested, linted, documented; no TODOs or placeholders.
- Prefer existing open-source tools; custom code is glue only (installer, registry, evals, templates).
- Any deviation from RESEARCH.md picks needs an ADR in `docs/adr/`.

## Security
- User decision: all services bind to 0.0.0.0, open to the LAN, no login. Runtime agents have full machine access (no approval gates).
- Telegram bot answers only allow-listed Telegram user IDs (it is internet-reachable).
- Docker socket is not exposed over TCP. Secrets only in `~/LAW/state/.env` (0600); gitleaks in pre-commit.
- The "Ask the user before" list applies to this build session, not to the runtime agents.

## Long-running work
- Foreground commands ≤ 10 min; downloads, soaks and model loads run in the background with logs in `~/LAW/logs/`.

## Scope
- In: coding, design, documentation, judgement, computation, image/video/audio generation and editing.
- Computer use: runtime agents can operate any app and the GUI (open apps, browse, download/install, play media, change volume and settings). See PROMPT.md P4b.
- Channels: Telegram (primary), `law` CLI (local and from LAN devices), Open WebUI, `law` API.
- Queue: Valkey + job framework with a model-aware scheduler (PROMPT.md P4b). No SQLite queue.
- Out: penetration testing (removed by the user on 2026-10-19).
