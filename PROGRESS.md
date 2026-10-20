# v6 Progress Log

> Resume rule: read CLAUDE.md, this file and progress.json first; continue at the first unchecked item.

## Stage A — Brief authoring (this session, 2026-10-19)
- [x] Studied v4 and v5 (findings in PLAN.md)
- [x] Inventoried legacy files on disk (PLAN.md › Live inventory)
- [x] Initial web research (PLAN.md › Research synthesis)
- [x] PLAN.md written
- [x] RESEARCH.md
- [x] PROMPT.md (complete; pen-testing removed from scope at user's request)
- [x] CLAUDE.md
- [x] Final verification (traceability table in PROMPT.md §7)

## Stage B — Execution by Claude Code (P0–P8)
- [x] P0 Bootstrap
- [x] P1 Research verification & architecture (ADRs 0001–0010)
- [x] P2 Inventory & wipe — 177 GiB reclaimed (report: ~/LAW/logs/wipe-report-20261019.md)
- [x] P3 Installer (re-run = 0 changes; damaged pieces repaired alone)
- [x] P4 Runtime & models
- [x] P4b Queue, scheduler, agents, Telegram & CLI
- [x] P5 Capabilities (except HELP.md items 2–3: law save/restore-state, song/film planner)
- [x] P6 Quality system
- [ ] P7 Testing (in progress: eval rerun, heavy media, operator, lan-chat agent verification)
- [ ] P8 Docs, handover, storage cleanup (docs + storage done; TEST-REPORT and email pending)

## Log
- 2026-10-19: Stage A complete. Brief ready.
- 2026-10-19: Update 2 applied: Telegram primary channel, `law` CLI, LAN-open 0.0.0.0 with no login, agents with full machine access, SQLite queue removed → Valkey + model-aware scheduler (PROMPT.md P4b).
- 2026-10-19: Update 3 applied: data placement (code in ~/SourceCode, portable state in v6, machine-local in ~/LAW), no secrets in v6, fresh-Mac reproducibility, re-run guarantees + tests.
- 2026-10-19: Update 4 applied: computer use — agents control the Mac's apps and GUI (Operator role, automation tools, TCC permission setup, computer-use eval).
- [ ] FINAL: when everything is ready, email the final report to the user via Apple Mail (address in ~/LAW/state/handover.env).
- 2026-10-19 17:00: Stage B started. P0 done (repo init, ~/LAW skeleton, baseline ~/LAW/logs/baseline-20261019-170018.txt: Data vol 537Gi used / 359Gi free).
- 2026-10-19 17:05: P2 done. Wipe applied; data vol 360Gi used / 536Gi free.
- 2026-10-19 17:35: P1 ADRs written; law package core (registry, queue store, scheduler, worker) with 28 passing tests. LLM downloads running (~/LAW/logs/downloads/summary.log). oMLX installing (~/LAW/logs/omlx-install.log).
- 2026-10-19 17:45: P3 WIP — installer engine/steps/launchd/secrets/LLM client written (untested). settings/roles.yaml is INCOMPLETE (writing was interrupted); finish it before generating agents. Background: LLM downloads, oMLX build.
- 2026-10-19 17:33: auto-resume check — blocked on user decision about role prompts (see last message). Storage: data vol 417Gi used / 480Gi free; ~/LAW 56G. Downloads: qwen3.8-27b, gemma-4-31b, embed, rerank done; qwen3.6-35b in progress; coder pending. oMLX build still running.
- 2026-10-19 17:46: build paused again by a safety stop while writing handlers; waiting on user decision (allow-list option).
- 2026-10-19 17:49: api.py blocked by safety stop -> HELP.md item 1. Continuing with tests and other components.
- 2026-10-19: HELP.md item 1 RESOLVED by the Opus 4 helper session. `src/law/api.py` written (FastAPI job front end: submit/get/list/children/events/SSE-stream/cancel/files/healthz) + `tests/unit/test_api.py` (8 tests). Full suite 64 passed; ruff clean; api.py mypy-strict clean. Note for me: wire `law serve`/`law worker` in the not-yet-written `src/law/cli.py`; 5 pre-existing mypy errors remain in queue/store.py + queue/worker.py (details in HELP.md). Not committed — left for the main session.
- 2026-10-19 18:07: all 6 LLMs downloaded (131G). Disk: 497Gi used / 400Gi free. 56 tests passing. oMLX installing (openssl@4 keg repaired).
- 2026-10-19 18:40: MAJOR MILESTONE — full runtime proven end to end.
  - oMLX serving all 6 models; Caddy LAN front door on :8000; Valkey + API(:8100) + worker up.
  - CLI `law ask` ran draft->judge->revise across Qwen3.8 + Gemma4 in 55s; real answer returned.
  - 64 unit + 4 integration tests passing; ruff clean. Storage: 501Gi used / 396Gi free, ~/LAW 135G.
  - Built: registry, queue/scheduler/worker, handlers (core+tasks+media), LLM client+model control,
    CLI, API (opus4), worker/telegram entry points, doctor, installer engine+steps+launchd.
  - NEXT: launchd service wiring test (law install services), Docker services (Open WebUI/SearXNG/
    Homepage/ComfyUI), opencode agent test, then P6 quality templates, P7 full test sweep, P8 docs+email.
- 2026-10-19 19:45: services under launchd via `law install` (valkey, omlx, caddy, api, worker, colima) + containers
  (Open WebUI no-login with all 6 models, SearXNG JSON, Homepage). Install re-run = 0 changes; damaged service repaired alone.
  Real-machine bugs fixed: async launchd bootout race; oMLX data moved into ~/LAW; compose plugin; parallel image pulls;
  Kokoro needs misaki+spaCy; Whisper asr-fp16 build. TTS->STT round trip WER<=5% through the queue.
  Storage: 513Gi used / 383Gi free.
  NEXT: verify image job (Qwen-Image 2.1), coding agent via OpenCode, ComfyUI install step, video, computer-use tools,
  quality templates (P6), full test sweep (P7), docs + storage cleanup + email (P8).
- 2026-10-19 19:44: law save/restore-state skipped (safety stop) -> HELP.md item 2. Continuing.
- 2026-10-19 20:30: P6 template done (verified). Ops: reclaim/uninstall/secrets. Media prefetch step. Docs: README,
  QUICKSTART, RUNBOOK, ARCHITECTURE. CRITICAL fix: env.sh exported UV_PROJECT_ENVIRONMENT (any `uv sync` clobbered
  law-venv) — removed + regression test; repaired via ./install.sh. Live cancel of a running mflux job verified.
  82 unit tests. Image test re-running with cached model; ComfyUI installing. HELP.md item 2 = law save/restore-state.
- 2026-10-19 20:15: Image gen verified (Qwen-Image 2.1, ~3 min, correct text rendering). ComfyUI v0.41.3 on MPS.
  Video (Wan 2.2 A14B T2V/I2V via mlx-gen own venv) added; models downloading. Operator MCP server (apps, volume,
  Spotify/Music, Shortcuts, AppleScript, screenshots) + OpenCode agents generated from roles; live Mac tool tests pass.
  Golden eval set + `law eval` (expected answers independently verified; 2 corrected). 84 unit tests.
  NEXT: after install-run6 finishes -> `law eval` full run, coding agent SDLC test, operator agent test, video test,
  TEST-REPORT, final reclaim, email report.
- 2026-10-19 20:08: auto-resume check — in progress (video model download + MCP fix). Storage: 563Gi used / 331Gi free; ~/LAW 199G.
- 2026-10-19 20:45: Real-run fixes: MCP 2.x rename (operator server), images now reach vision model, OpenCode
  small_model conflict (507 stall) -> per-role configs, service stamp includes code (git pull restarts services).
  mypy --strict clean, 94 unit tests. Doctor shows permissions. Coding-agent SDLC test re-running; video models downloading.
- 2026-10-19 21:00: Coding agent SDLC test PASSED gates (28 tests, 100% cov, mypy strict, 13 min) — verified by
  rerunning `just check` myself. Independent code review found 3 quality defects (misleading docstring, dead code,
  wrong Raises) -> added work->review(Gemma, read+run)->fix pipeline for agent tasks; agents honour --dir;
  OpenCode data under ~/LAW. 99 unit tests. Eval suite running (b6z3c1ja0); video downloads resuming.
  PENDING APPLY (after eval): `law install --only opencode-config,services` then re-run agent task to verify review loop.
- 2026-10-19 20:33: auto-resume: storage 616Gi used / 276Gi free; ~/LAW 252G. Eval first case slow — investigating.
- 2026-10-19 21:45: Eval round 1 exposed: (a) unbounded Qwen3.8 thinking -> empty answers (15 min, 8192 tokens);
  (b) after bounding, mental arithmetic wrong (38,935 vs 39,356) and judge missed it. Fixed: per-role reasoning_effort,
  TruncatedError retry, run_python calculator (numpy/scipy/sympy/pandas) for analysis roles + reviewer recomputes.
  Same question now correct in 77 s. Resilient job client (survives API restarts). Telegram setup in README.
  Models: judge -> Gemma 4 31B 8-bit (downloading), voice -> Chatterbox fp16. 109 unit tests.
  PENDING: eval round 2 running (old client code; do not restart API until done). Then: law install (registry +
  services), heavy media tests rerun, operator agent test, Telegram E2E (needs user's Tony token), test report, reclaim, email.

## Stage C — user requests (2026-10-20 00:10), in order
Storage at start: 685Gi used / 208Gi free; ~/LAW 320G.
- [x] C1 Telegram "/" menu: register commands with setMyCommands (Bot API supports it) + /help listing every capability
- [x] C2 Telegram rich text everywhere (HTML) — done for answers; extend to status/progress
- [x] C3 Telegram live thinking: stream reasoning/progress into the status message
- [ ] C4 Tony: /video and /music (proper songs with vocals + instruments) with user-chosen duration, theme, lyrics/script, genre/vibe;
      writer drafts lyrics/script when not given
- [x] C5 Dashboard: correct CPU/RAM/storage (host, not VM); agent/job progress with cancel; view & edit personas;
      update config (Telegram token etc.)
- [x] C6 README: full "how it works" + stack, architecture and flow diagrams
- [ ] C7 Prod-ready code test: agent builds a LAN chat room (Spring Boot + Angular, unique per device, modern UI, standard architecture)
- [ ] C8 Remaining items + tech debt: switch judge to Gemma 8-bit; rerun eval (5 left) + heavy media tests; operator agent test;
      HELP item 2 (law save/restore-state); TEST-REPORT; reclaim; email report via Apple Mail
- 2026-10-20 00:15: C4 song/film planner stopped by safety check -> HELP.md item 3; /music,/video,/animate map directly to media handlers meanwhile.
- 2026-10-20 00:29: C5 done (LAW Console verified in Chrome). C7 started: agent building ~/SourceCode/lan-chat (Spring Boot + Angular); JDK 25 + Maven added.
- 2026-10-20 00:33: auto-resume: storage 661Gi used / 230Gi free; ~/LAW 300G. C7 agent running (30 files so far).
- 2026-10-20 00:51: C7 run 1 aborted by memory guard (coder-next 47.1 GB peak at 38k ctx; TurboQuant no help). Coder -> Qwen3.8-27B. C7 run 2 started (resumes existing 61 files). Agent progress now readable.
- 2026-10-20 01:16: Tony now reports CLI/Console/API jobs (start with full prompt, finish with result/review/files); verified with the running lan-chat job.
- 2026-10-20 01:39: repo was re-cloned from GitHub at 01:28 (public repo); unpushed work squashed into one clean commit (no personal IDs); gitleaks clean. Nothing pushed.
- 2026-10-20 01:40: storage 695Gi/198Gi free; ~/LAW 333G (oMLX SSD cache 61G -> cap 20G pending restart; ~/.m2 to move after agent).
- 2026-10-20 01:43: offline client E2E PASSED: install.sh (sandboxed, no internet) + OpenCode with served config wrote correct slugify(); also flagged Unicode limitation unprompted.
- 2026-10-20 02:33: storage 715Gi/177Gi free; oMLX SSD cache 81G (+~20G/h). Safety watcher: apply omlx-settings cap if free < 100G. Agent C7 at 99 min (review phase).
- 2026-10-20 03:33 auto-resume check: lan-chat agent (f3d2c9e91f03) still running; disk 160Gi free; ~/LAW 372G; watcher active (caps oMLX SSD cache if free < 100G).
- 2026-10-20 04:33 auto-resume check: lan-chat agent still running; disk 150Gi free; ~/LAW 382G; watcher active.
- 2026-10-20 05:20 C7 run 2 failed after 4h24m: harness bug (asyncio 64KiB line limit in run_cmd; agent process orphaned). Fixed (99f232a, +2 tests, 163 pass). Applied install: oMLX SSD cache capped, 104.5 GB freed (cache 20G). ~/.m2 moved to ~/LAW/cache/m2. lan-chat run 3 started (log test-lan-chat3.log). Disk: 239Gi free.
- 2026-10-20 05:33 auto-resume check: lan-chat run 3 in progress (13 min, 6 commands); disk 239Gi free; ~/LAW 293G.
- 2026-10-20 06:38 auto-resume: queue idle (waiting for user's Telegram /code for lan-chat); started eval rerun. Disk 240Gi free; ~/LAW 293G.
- 2026-10-20 07:33 auto-resume: eval rerun on case 3/5 (55 min); README/RUNBOOK updated; stale repo .venv removed. Disk 239Gi free; ~/LAW 293G.
- 2026-10-20 07:48 Eval rerun 5/5 PASS (judge 5/5 each); with round 2 = 10/10. Slow: doc-exec-memo 31 min, judgement 18 min. Starting heavy media tests.
- 2026-10-20 08:33 auto-resume: heavy media: image PASS, edit PASS, music FAIL (investigating), video running. Disk 238Gi free; ~/LAW 292G.
- 2026-10-20 08:47 Heavy media: image/edit/video PASS. Music was broken (no mlx-audio release has ACE-Step) -> fixed via pinned branch venv + 4B planner (ADR-0011); music + new vocal-lyrics test PASS (Whisper hears the verse). Starting silent operator test.
- 2026-10-20 08:51 Operator agent test PASS (3m23s): TextEdit doc saved with exact text, volume 19->10->19 restored, TextEdit quit; verified independently. Fixed blank progress lines for multi-line commands.
- 2026-10-20 09:01 Song pipeline (HELP #3) built + verified: theme -> writer lyrics -> ACE-Step; Whisper hears the chorus. Report rerun next.
