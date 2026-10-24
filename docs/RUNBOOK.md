# Runbook

## Daily use

| Task | Command |
|---|---|
| Health + LAN addresses | `law doctor` |
| Live stats, jobs (cancel), personas, settings | LAW Console: `http://<mac-name>.local:8100` |
| One-line status | `law status` |
| Recent jobs | `law jobs` |
| Talk to Tony | Just type in Telegram: Tony remembers the recent conversation (`/new` starts over), answers short questions in seconds, and hands work that needs files, code, documents or apps to an agent that reports in the same chat (ADR-0017) |
| Send a file to yourself (what agents use) | `law send report.pdf` (Telegram) · `law send report.pdf --email --subject "Report"` (Mail.app; `--to` for another address) |
| Real API of a library symbol (what coding agents use instead of guessing; ADR-0016) | Java `law api StompSession subscribe` · Go `law api net/http.Client Do` · TS `law api @angular/core:signal` · Swift `law api View navigationTitle` · Monkey C `law api WatchUi.View` |
| Deploying while agents work | `law install` prints "restarts on the new code between steps" | Nothing to do; `LAW_FORCE_RESTART=1 law install` restarts at once (interrupts the step) |
| Disk filling up | Console → **Storage** (sizes per cache) | Clear a cache or all LAW caches there, or `law reclaim`; shorten retention in the same tab |
| Is an agent working, thinking or stuck? | Console → **Agents**, or `/jobs <project>` in Telegram |
| A job failed or got stuck | **Retry** (from the failed step, earlier work kept), **Skip** (carry on without that step) or **Restart step** (stuck); buttons in the Console and Telegram, or `law retry <id>` |
| Cancel a job | `law cancel <id>` (cancelling a pipeline also stops its running stage and agent process) |
| Service log | `law services logs worker` (also `api`, `omlx`, `caddy`, `valkey`, `telegram`, `comfyui`) |
| Restart a service | `law services restart worker` |
| Models on disk | `law models list` |

All logs are in `~/LAW/logs/`.

## Change settings

Everything is declared in `settings/` (git):

- `settings/registry.yaml`: models, memory budget, ports, media tools, pinned versions
- `settings/roles.yaml`: personas and their tools
- `compose.yaml`: Open WebUI, SearXNG, Homepage
- `Brewfile`: system packages

Edit, commit, then run `./install.sh` (or `law install`). It applies only the difference:
registry changes restart the API, worker and Telegram bot; other services restart only when their own settings change.
Preview first with `law install --dry-run`.

### Swap or add a model

1. Find an MLX build on Hugging Face and note its commit SHA (revision).
2. Add or edit an entry under `llm_models:` with `repo`, `revision`, `mem_gb`, `lane`, `roles`.
3. `law install`. The download is verified and marked complete; `law reclaim` later frees the old one.

Keep every heavy model's `mem_gb` within `machine.model_budget_gb` (48 GB); the registry refuses anything larger.

## Personas

Edit a role's instructions, temperature, reasoning effort or tools in the Console (Personas tab) or in
`settings/roles.yaml`. Saving in the Console validates the change, keeps the file's comments and re-applies the agents.

## Secrets

Secrets live only in `~/LAW/state/.env` (mode 600) and never in git.

```bash
law secrets set LAW_TELEGRAM_TOKEN 123456:ABC...
law secrets set LAW_TELEGRAM_USER_IDS 11111111
law install                                 # starts or restarts the Telegram bot
# or: LAW Console → Settings (checks the token with Telegram first)
law secrets export ~/Desktop/law-secrets.env   # keep this in your password manager
```

## Storage

```bash
law reclaim --dry-run   # preview
law reclaim             # remove unused model caches, old trash, temp files; prune uv/Homebrew/Docker caches
law save                # export Open WebUI presets/prompts/tools/knowledge to settings/openwebui (secret-scanned, committed, not pushed)
law restore-state       # add the saved ones that are missing on this Mac (also runs in law install)
du -sh ~/LAW/*          # where the space goes
```

Models are the bulk (about 200 GB). Generated media accumulates in `~/LAW/outputs/`; delete what you don't need.

## Fresh Mac or after a format

1. Install Homebrew and Python.
2. `git clone <repo> ~/SourceCode/OpenSourceLocalAI && cd ~/SourceCode/OpenSourceLocalAI/v6`
3. `./install.sh --secrets <your exported secrets file>`
4. `law doctor`. The first time the operator role controls an app, approve the macOS permission prompts
   (System Settings → Privacy & Security → Accessibility, Automation, Screen Recording).

## Reset

```bash
law uninstall --system   # stop and remove services, containers and the ~/.zprofile block
rm -rf ~/LAW             # remove all machine-local data (models, outputs, secrets)
```

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `law doctor` shows a service down | `law services logs <name>` | `law install` repairs it; or `law services restart <name>` |
| An agent step pauses for a long time with no model activity | `pmset -g log \| grep Sleep` | The Mac slept. The worker keeps it awake while jobs run (`caffeinate`), but closing the lid on battery still sleeps it: keep the lid open or the charger plugged in during long jobs |
| Jobs stay queued | `law jobs`; worker log | A heavy job may be running (one at a time by design). `law services restart worker` requeues safely |
| Very slow first answer | Model is loading (about 0.25 s per GB) | Normal after a swap; repeated jobs on the same model are fast |
| Open WebUI page empty | `law services logs` for Colima; `law install` | First start takes about a minute |
| Out of memory warnings | Activity Monitor → Memory | Lower `context` or `mem_gb` in the registry, or close large apps |
| Image pull errors during install | Network inside the VM | Re-run `law install`; it resumes and pulls one image at a time |
| Operator can't control an app | System Settings → Privacy & Security | Allow the terminal / `law` under Accessibility and Automation, then retry |
| Open WebUI says it cannot load a model / 507 | A heavy job (agent, image, video) is using the memory | By design on 64 GB: only one large model fits. Pick the model already loaded, or retry when `law jobs` shows the job finished |
| Agent progress says "no file changed for 25 min: nudging" | The agent was re-reading instead of building | Automatic: it resumes the same session told to build and fix. Per job, `stall_min` in the payload changes the limit (0 turns it off) |
| `/proposal` fails with "Mail did not respond" or "not allowed" | macOS asks once whether LAW may control Mail | System Settings → Privacy & Security → Automation: allow it for Mail, then resend; the PDF path is in the error |
| `/proposal` fails with "no report email set" | `LAW_REPORT_EMAIL` missing | Set it in the Console (Settings → Report email) or re-run `./install.sh` |
| A diagram in a proposal shows as code | Mermaid could not draw it | The email names it; fix the block in `docs/proposal/proposal.md` and re-run `pandoc` or ask Tony to fix it |
| Video job very slow | Expected: Wan 2.2 14B renders ~5 s of video in several minutes | Use shorter clips (`--seconds 3`) or lower resolution in the job payload |

## Developing LAW itself

The installer's venv has every dependency, so use it for checks (no separate repo venv is needed):

```sh
P=~/LAW/runtime/law-venv/bin
$P/ruff check src tests && $P/mypy src && $P/python -m pytest -q tests/unit
python3 scripts/test_report.py   # every layer, incl. live integration; writes docs/TEST-REPORT.md
```

After changing `src/`, `law install --only services` restarts the API, worker and Telegram bot.
