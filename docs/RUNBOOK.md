# Runbook

## Daily use

| Task | Command |
|---|---|
| Health + LAN addresses | `law doctor` |
| Live stats, jobs (cancel), personas, settings | LAW Console: `http://127.0.0.1:8100` |
| Approve a plan or build | Console, Projects: the button in Waiting for approval (same as `/go` or `/allgood`) |
| Redo a project's plan, proposal and tickets | Tell Tony. Agents write the new version and tickets (the old ones leave the board) and it waits for your go |
| See the Console from the internet (read only) | VS Code Ports panel: forward **8110**, keep visibility Private. Never forward 8100: it has no login and can change everything (ADR-0022) |
| One-line status | `law status` |
| Recent jobs | `law jobs` |
| Talk to Tony | Just type in Telegram: Tony remembers the recent conversation (`/new` starts over), answers short questions in seconds, and hands work that needs files, code, documents or apps to an agent that reports in the same chat (ADR-0017) |
| QA a project now | `/qa <project>` in Telegram or `law qa <project>`: black-box test of the running product, open bugs listed by severity. Only on request: QA never runs on a schedule |
| Fix every open bug | `/bugfix <project>` (no text) or `law bugfix <project>`: fix, then QA retest, up to 3 rounds |
| Report a bug in a project | `/bugfix <project> <what goes wrong, what you expected>` in Telegram: logged in `<project>/.law/BUGS.md` and on the board; the agent fixes it, then QA retests the running product (ADR-0018) |
| Start completely fresh | `/hardreset` in Telegram (asks first): stops all jobs, deletes job history, agent conversations, Tony's chat memory and every LAW cache. Keeps models, settings, secrets, projects with their `.law/`, generated files, and the jobs of unfinished projects (Retry still continues them). Console: `POST /reset` |
| Send a file to yourself (what agents use) | `law send report.pdf` (Telegram) · `law send report.pdf --email --subject "Report"` (Mail.app; `--to` for another address) |
| Message yourself from an app in a container | `POST http://host.docker.internal:8100/send` with `{"text": "..."}` (Telegram, allow-listed chats; 503 if Telegram is not set up) |
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
- `compose.yaml`: Open WebUI, SearXNG, Homepage, Portainer (ADR-0019)
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
A role's temperature applies only to a model without `sampling` in `settings/registry.yaml`; the three LLMs run at
their model cards' sampling (ADR-0020). A new model gets its card's values there.

## Project boards

The Console's Projects tab shows one board per project (pick it in the dropdown), with the columns To do, In
progress, Waiting for approval, Completed and Failed / cancelled. The board is built from what LAW already records,
so it never needs updating by hand:

- proposal writing, its review (QA) and delivery; the environment stage; each QA review round and the fix round
  for the bugs it reported; the handover document: from the project's pipeline stages and their logs;
- go / no go and verification gates: Waiting for approval until the owner decides in Telegram;
- development tickets: `.law/PLAN.md` (`- [ ] P-6 ...` under `### M2 ...`), with their commits (`P-6: ...`),
  `.law/PROGRESS.md` entries and the agent's actions while the ticket was open.

Click a ticket for its details, steps, notes and logs. Notes are stored in `~/LAW/state/boards/<project>.json`. A note
names its ticket, or is pinned to the ticket in progress when it was written (Telegram `/note` works this way):

```bash
law board show pantry
law board note pantry "P-17: container apps use POST /send" --ticket P-17 --author "Claude (build session)"
law board note pantry "use the PEM files" --to-agent      # the ticket in progress; the agent gets it too
```

Delete project (Console) removes its board notes, its jobs with their logs and its LAW record; the code folder stays
unless you tick the box, which moves it to `~/LAW/trash` (deleted after the trash retention). It is refused while
one of the project's jobs runs.

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

Models are the bulk (about 200 GB). Generated files accumulate in `~/LAW/outputs/`: Console → Storage →
"Generated files older than a week" moves them to the trash (emptied after 7 days).

## Hardware wear

Checked 2026-11-23 on the M5 Pro (1 TB SSD, 64 GB):

| Part | Reading | What LAW does |
|---|---|---|
| SSD | 2% of rated life used, 0 integrity errors, 100% spare; about 190 GB written per day over the last 11 days (model downloads and quantizing included) | The oMLX prompt cache is capped at 20 GB; unused models and media are deleted; at this rate the SSD lasts decades |
| Battery | 100% capacity after 48 cycles; macOS holds it at 80% on AC | Leave the charge limit on while the Mac stays plugged in |
| Display | Was kept on day and night by a keep-awake command | LAW keeps the display awake only while a job runs (computer use needs it); otherwise it may sleep |

Check again:

```bash
smartctl -a disk0 | grep -E "Percentage Used|Data Units Written|Media and Data"   # brew install smartmontools
system_profiler SPPowerDataType | grep -E "Cycle Count|Condition|Maximum Capacity"
ioreg -c IOBlockStorageDriver -r -w0 | grep -o '"Bytes (Write)"=[0-9]*'            # bytes written since boot
```

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

Docker services down after a restart: `tail ~/LAW/logs/colima.log`. The Colima launchd agent clears pid files that no
longer belong to `limactl`, the cause of "vz driver is running but host agent is not".

A project made by hand (it has `.law/project.json`) and missing from Console → Projects: `law adopt <folder>`. It shows
its tickets and waits for your `/go <project>`. Agent tasks do this themselves when they finish.

QA fails a round that has no evidence: no test report, no checks in `qa/`, or, for a project with `docs/design/`, no
design review of the screens. The designer role does that review because QA's model cannot see images. Owner notes sent
during the handover wait for the next build session, and the verify message lists them.


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
| A ticket session's progress says "split the ticket into smaller steps" | Second stall in a row in one ticket session; in a coder's ticket only project files count as saved work, not `.law/` notes | Automatic: the agent splits it into `<ID>.1, <ID>.2 ...` (any indent); each sub-ticket gets its own session, and LAW ticks the parent when all parts are ticked |
| Progress says "nudging the agent to tick and commit" | On a stall with uncommitted product files LAW ran `.law/check.sh` itself and it passed | Automatic. The gate run redeploys the project's stack when its `check.sh` does (Pantry's does) |
| Progress says "debugging with probe files" or "edited a file with a shell command" | A second probe/scratch file in one session; a heredoc, `sed -i`, `perl -i` or `tee` write | Automatic reminders, once per session; not strikes |
| Progress says "OpenCode's database is busy: retrying" | OpenCode's store under `~/LAW/data/opencode` was still locked by the previous session | Automatic: retries after 10, 30 and 90 s, then fails the step (Telegram offers Retry) |
| A pipeline ran past its planned build stages | Tickets were still open when the last build stage ended (ADR-0021) | By design: LAW adds a build stage, up to 64 stages in all; a stage that made no progress goes on to review instead |
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

The repo is public, so its hooks keep personal data out (ADR-0023). `law install` points git at `.githooks/`:
pre-commit and commit-msg run gitleaks with `.gitleaks.toml` (default secret rules plus email, home-path, Mac-hostname
and LAN-address rules), and pre-push re-checks every pushed commit and refuses any author or committer other than
the GitHub noreply address. CI (`.github/workflows/privacy.yml`) runs the same checks over all history. Use
`law.paths.display()` / `law.privacy.scrub()` for anything written into committed files. If a hook blocks you, fix
the content rather than adding an allowlist entry.
