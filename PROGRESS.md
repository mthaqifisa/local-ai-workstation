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
- [~] C4 Tony: /video and /music (proper songs with vocals + instruments) with user-chosen duration, theme, lyrics/script, genre/vibe;
      writer drafts lyrics/script when not given
      → songs done + verified (/music, law song); films built + unit-tested (/film, law film, ADR-0012); real film render pending GPU
- [x] C5 Dashboard: correct CPU/RAM/storage (host, not VM); agent/job progress with cancel; view & edit personas;
      update config (Telegram token etc.)
- [x] C6 README: full "how it works" + stack, architecture and flow diagrams
- [~] C7 Prod-ready code test: agent builds a LAN chat room (Spring Boot + Angular, unique per device, modern UI, standard architecture)
- [ ] C8 Remaining items + tech debt: switch judge to Gemma 8-bit; rerun eval (5 left) + heavy media tests; operator agent test;
      HELP item 2 (law save/restore-state); TEST-REPORT; reclaim; email report via Apple Mail
      → done: judge 8-bit, evals 10/10, heavy media 5/5, operator test, HELP 1-3, TEST-REPORT. Left: reclaim, email
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
- 2026-10-20 09:35 auto-resume: report on heavy video test; lan-chat /code (Telegram, job 2ee5c80be561) queued; fixes (save/restore-state, queued status, silent tests, secrets.py tracked) on branch tony-fixes awaiting merge after report. Disk 228Gi free; ~/LAW 302G.
- 2026-10-20 09:53 TEST-REPORT generated: all gates PASS (unit 181, integration 9, heavy media 5, installer re-run 0 changes), evals 10/10. Merged tony-fixes (law save/restore-state = HELP #2 done; queued status; silent test jobs; secrets.py tracked). lan-chat agent (Telegram job 2ee5c80be561) running. Next: verify lan-chat independently, then final scan + email.
- 2026-10-20 10:33 auto-resume: lan-chat agent running (step 1 committed: initial snapshot). Disk 226Gi free; ~/LAW 304G.
- 2026-10-20 11:33 auto-resume: lan-chat agent 1h35m, 82 steps, no gate run yet (only initial commit). Lightning LoRAs downloaded. Disk 224Gi free; ~/LAW 307G.
- 2026-10-20 12:33 auto-resume: lan-chat run 5 (reasoning-effort fix) reached ./mvnw verify in 8 min (runs 1-4 never did); running. Disk 223Gi free; ~/LAW 307G.
- 2026-10-20 13:33 auto-resume: lan-chat run 5 at 1h23m: wrapper added, 3 edits (domain Message/User), still in backend fix loop; no gate commit yet. Disk 222Gi free; ~/LAW 309G.
- 2026-10-20 14:35 auto-resume check: lan-chat run 5 still running (backend fix loop, ~9 tok/s, steps 1-15 min; no gate commit yet). Disk: 224Gi free; ~/LAW 307G.
- 2026-10-20 15:25 lan-chat run 5 cancelled: no edits after 12:59 (re-read loop across context compactions); backend only 9 compile errors left. Run 6 (root 1a17f710b7a6) sent via Telegram with a tight compile-fix loop prompt. Follow-up idea: stall guard in task_work (N minutes without a write -> nudge with latest build output).
- 2026-10-20 15:30 stall guard (32e41eb) + docs; gitleaks false positive ignored (scan clean); HELP.md film line fixed. Handover email drafted.
- 2026-10-20 15:34 auto-resume check: lan-chat run 6 active (backend compiles; now on tests). Disk 223Gi free; ~/LAW 307G.
- 2026-10-20 16:02 run 6 fixed all 9 compile errors (15 min), then stalled on tests (Boot 3.3.4 vs Java 25 bytecode, bad logging.level keys, missing id generation, username case). Cancelled near context limit; run 7 (root 3749d7abaa03) sent via Telegram with the 4 diagnosed causes. The email will say which fixes were diagnosed by me and which the agent found.
- 2026-10-20 16:33 auto-resume check: lan-chat run 7 active (Boot 3.5.16 + logging fixed; 19/35 tests failing: missing @Service, id generation, username case). Disk 224Gi free; ~/LAW 307G.
- 2026-10-20 17:10 built: translator role + brief stage, <project>/.law/ memory with session resume, deterministic project folders (fix for /code writing into ~/SourceCode), report email (install prompt + Console), /proposal pipeline (research, 15-section doc, review, mermaid+pandoc+typst PDF, Mail.app), doc-tools step. 225 unit + real render test pass. Not deployed yet (law install would kill run 7).
- 2026-10-20 17:12 run 7: 19->13 failing tests, then lost the @Service finding to compaction. Cancelled; law install (14 changes, re-run 0, doctor healthy) deployed stall guard, translator, .law/, /proposal, report email, doc-tools. Run 8 (root 946629da3287) sent via Telegram; first stage is the translator.
- 2026-10-20 17:33 auto-resume check: lan-chat run 8 active (translator brief in .law/, build loop; coder reasoning now low). Disk 222Gi free; ~/LAW 307G.
- 2026-10-20 17:38 user cancelled lan-chat: run 8 cancelled; ~/SourceCode/lan-chat, its brief, scaffold and logs deleted. Next: project lifecycle flow (/code init template -> proposal -> go/no go -> env check -> milestones -> review rounds -> verify -> enhance loop), then E2E from Telegram.
- 2026-10-20 17:43 deployed project lifecycle (install 33 changes, re-run 0, healthy). E2E started from Telegram: /code init -> template -> filled 'LAN Chat' template -> project_new 94465b386c13 (proposal v1). Plan per user: at Go/No go press No go with a minor tweak to test the pivot.
- 2026-10-20 18:33 auto-resume check: E2E proposal v1 in research (resumed its session after a deploy: 128K context, write-as-you-go). Fixes today: translator sees fixed spec; Telegram progress without link previews. Disk 222Gi free; ~/LAW 307G.
- 2026-10-20 19:33 auto-resume check: E2E proposal v1 still in research (researcher, after deploy resumes); Agents tab + /jobs + Retry/Skip/Restart shipped. Open question to user: job retention policy. Disk 222Gi free; ~/LAW 307G.
- 2026-10-20 19:51 E2E: research done (research.md 2.6K words, 29 sources); stopped the re-verifying retry and pressed Skip in Telegram (Retry/Skip buttons verified); writer stage running. Committed but NOT deployed (needs worker restart after the proposal): LAW-recorded PROGRESS.md entries (dd2...).
- 2026-10-20 20:36 auto-resume check: specs now resolved at run time (spec_ref); lan-chat pipeline migrated, writer retried on current rules (stage dc008c9f1132). Disk 222Gi free; ~/LAW 307G.

### E2E plan (project lifecycle, functional run: thinking low, proposal brief) — 2026-10-20 evening
Happy path and unhappy paths, all from Telegram, monitored every 5 min:
- [x] Incomplete template -> Tony names the missing fields (21:54)
- [x] /code init -> template -> filled template -> project folder, git, .law/ (21:55, root 86d87bf7fed9, low/brief)
- [x] Proposal v1 (brief) PDF in Telegram + Go / No go buttons (01:34; email blocked: Mac locked, needs one Automation click; review 5/5 after prose-verdict fix)
- [x] Wrong-time decision (Go while v2 is written) -> refused, state unchanged (now an info message, not a failure)
- [x] No go with a pivot (DMs + 200-message history) -> v2 reflects it fully ('What changed' section, effort, risks, new DM wireframe), PDF in Telegram (03:49)
- [ ] No go + "stop" -> parked; /resume <note> -> revived (v3)
- [x] Go -> environment stage: JDK 21 installed via brew and pinned in .law/env.sh; ENVIRONMENT.md; committed (04:41)
- [~] Milestones on a ticket board: Build version 2, M1-M5, LC-1..LC-17 with acceptance; LC-1 committed + independently green (05:18)
- [ ] Deploy during a step -> worker restarts only between steps
- [ ] Retry / Skip / Restart / Cancel exercised at least once on real steps
- [ ] Review/fix rounds -> handover document with real screenshots, emailed + posted
- [ ] Verify gate -> Needs changes (new requirement) -> enhanced proposal v(n+1) -> Go -> incremental build
- [ ] All good -> done; /projects shows it
- [ ] Independent verification by Claude (build, tests, run, screenshots) + email to owner
- 2026-10-20 21:23 oMLX reinstalled --with-custom-kernel (all 5 kernels load). Verified: identical temperature-0 answers; fresh 17K-token prompt 41.4 s vs 39.7 s before (no measurable gain for Qwen3.8); repeated long prompt 2.4 s from the SSD prompt cache. Film rerun started (LoRA fix).
- 2026-10-20 21:33 auto-resume check: film rerun in progress; then reclaim and E2E. Disk 218Gi free; ~/LAW 307G.
- 2026-10-20 21:58 film test PASSED after the LoRA fix: 10.0 s, 2 shots + score, h264 832x480 + aac, 1,823 s end to end (~14 min per 5 s shot). law reclaim: nothing stale (~1 GB Homebrew/Docker); 218Gi free, ~/LAW 307G. Pre-E2E list complete; starting the E2E plan.
- 2026-10-20 22:33 auto-resume check: E2E proposal v1 research in progress (writing research.md topic by topic). Disk 218Gi free; ~/LAW 310G.
- 2026-10-20 23:33 auto-resume check: proposal now written one session per section (fix deployed; run migrated, section 1 running). Disk 221Gi free; ~/LAW 307G.
- 2026-10-21 00:33 auto-resume check: proposal sections 1-2 written, 3 running; fixes today: thinking 'off' for sections, drain-before-restart, section resume prompt. Disk 221Gi free; ~/LAW 307G.
- 2026-10-21 01:45 E2E fixes: section-per-session writing with thinking off (sections 4-7 ~4-8 min each vs 10-45), drain-before-restart, prose verdicts read, diagrams drawn before review, email failure non-blocking, Tony follows externally submitted Telegram jobs, wrong-moment decisions answered as information.
- 2026-10-21 01:40 auto-resume check: E2E proposal v2 (pivot) in progress. Disk 221Gi free; ~/LAW 307G.
- 2026-10-21 02:33 auto-resume check: proposal v2 revising sections (wireframes now; new DM wireframe). Disk 221Gi free; ~/LAW 307G.
- 2026-10-21 03:35 auto-resume check: v2 diagram auto-repair running live (3,4 fixed; 6 in progress). Disk 221Gi free; ~/LAW 307G.
- 2026-10-21 03:53 v2 fixes: diagrams auto-repaired (verified by drawing, exact mmdc errors), repairs on later tries saved, gate explains diagram fixes. Go pressed (API; screen locked) -> development v2 started (env stage).
- 2026-10-21 04:33 auto-resume check: development v2 environment stage (JDK 21 installed + pinned via .law/env.sh). Disk 220Gi free; ~/LAW 307G.
- 2026-10-21 05:33 auto-resume check: development v2 LC-2 in progress; LC-1 verified green on a clean clone. Disk 219Gi free; ~/LAW 307G.
- 2026-10-21 06:34 auto-resume check: dev v2 LC-2 restarted with a lead note in .law/PROGRESS.md; library-dig guard + Telegram flood fix deployed. Disk 218Gi free; ~/LAW 308G.
- 2026-10-21 07:34 auto-resume check: E2E dev (lan-chat LC-2) running; fixed dashboard false 'stalled' while the model generates; HTML-styled emails shipped and v2 proposal re-sent. Storage: / 221Gi free, ~/LAW 308G.
- 2026-10-21 08:34 auto-resume check: LC-2 at 2h — Origin tests green, broker tests 3/5 failing (hand-rolled STOMP client + subscribe race); lead note added, step restarted. Storage: / 221Gi free, ~/LAW 307G.
- 2026-10-21 09:31 PAUSED for a laptop restart (owner request: 2 days of sustained load). Dev run 2292ed0603bb cancelled cleanly; model idle; queue empty; all com.law6.* agents RunAtLoad. Resume with Retry on 2292ed0603bb (see progress.json next_step). Shipped today: HTML-styled emails (v2 proposal re-sent 07:04), dashboard health uses the stall-guard generation grace.
- 2026-10-21 09:33 auto-resume check: paused for the owner's laptop restart (no action). Storage: / 221Gi free, ~/LAW 307G.
- 2026-10-21 11:00 RESUMED after the laptop restart: law doctor all healthy (all com.law6.* agents up); Retry on 2292ed0603bb -> stage f611f87f67e8 (LC-2) running with the uncommitted WebSocket work intact.
- 2026-10-21 11:42 LC-2 coder stuck 25+ min digging jars after guessing Spring STOMP APIs (StompFrame, startSubscription, handleReceipt). Owner rule: agents fix projects; I train them. Root cause in our setup: the prompt banned API lookup but the compiler never shows the real API. Built `law api <Class> [member]` (javap signatures from the Maven test classpath + closest real names), pointed the coder prompt and both nudges to it, widened the dig guard (it had counted only 2 of 5 dig commands). 312 unit tests pass. Deploying.
- 2026-10-21 12:26 Restart step (API; from now on via Tony in Telegram per owner) -> 523cbcc340d8: the coder found `law api` on its own 3 min after the compile errors and used it correctly (StompSession, StompFrameHandler, Subscription, Receiptable, ...). An apparent 31-min stall 11:50-12:21 was the Mac idle-sleeping on battery: LAW had no keep-awake of its own (the caffeinate came from the Claude Code session). Fixed: the worker holds `caffeinate -i -s -w <pid>` while any job runs (c21f295, 316 unit tests, deployed; active from the next step). Owner rules saved: agents fix projects; agent commands only via Tony in Telegram.
- 2026-10-21 13:17 LC-2 solved by the coder alone after the law api fix: compile errors fixed via law api lookups (12:45-12:50), then found that the simple broker sends no SUBSCRIBE receipt and switched to broadcast-until-received; all tests green (Broker 4/4, Origin 14/14, context 1/1). One 10,233-token reply (21 min) stayed under the 16K output cap. Leftover unused receipt scheduler for its review to catch. Next: LC-2 commit.
- 2026-10-21 13:41 LC-2 committed by the coder (530286d) and independently verified on a clean clone: mvn verify exit 0, 19/19 tests. Watch items: the unused receipt ThreadPoolTaskScheduler left in BrokerIntegrationTest (dead code; does the review round catch it?); ~20 min of .law bookkeeping between green and commit (13:16-13:36). Next: LC-3.

### Multi-stack capability (owner request 2026-10-21 13:55): Go, Swift, Node.js, Angular, React, React Native; iOS + Garmin apps
Machine: Xcode + iOS simulator runtimes 26.5-27.1, Swift 6.4, Go 1.26, Node 26, Maven present. Missing: Garmin Connect IQ SDK, CocoaPods, XcodeGen.
- [x] `law api` per ecosystem (nearest project marker wins): Java/Maven (done), TypeScript/JS (Node, Angular, React, React Native via the TS compiler on .d.ts), Go (`go doc`), Swift (SDK symbol graphs: SwiftUI, UIKit, Foundation, ...), Monkey C (Connect IQ SDK)
- [x] Coder prompt + guards generalised to every stack (no guessing APIs; no digging node_modules/SDK internals)
- [x] Environment stage knows each stack: iOS (Xcode, simulator, XcodeGen project.yml instead of hand-edited .pbxproj, xcodebuild test), React Native (CocoaPods, Metro), Garmin (Connect IQ SDK, developer key, monkeyc, simulator)
- [x] Handover screenshots per platform: web (Chrome), iOS (`simctl io screenshot`), Garmin (simulator window capture)
- [x] ADR-0016 multi-stack development
- [ ] Capability proof, independent checks: small real projects per stack built by the agents from Telegram (after lan-chat frees the heavy lane)
- 2026-10-21 13:56 Owner: cancel + scrap the coding. Cancelled via Tony in Telegram (Cancel button; root 2292ed0603bb + stage cancelled; worker restarted on new code, keep-awake live). lan-chat reset to the proposal commit 07f8303 (code kept on branch backup/v2-coding-2026-10-21); .law/ rebuilt at 'awaiting_go' without env/build notes or the old session. Next: final tweaks (multi-stack), then Go from Telegram.
- 2026-10-21 14:05 Owner: memory still high -> the 27B coder stayed loaded (29 GB) after the cancel; oMLX never unloaded idle models. omlx-settings now sets idle_timeout_seconds = 300 (owner: free RAM after 5 min without agent work); applied, model unloaded, 90% RAM free.
- 2026-10-21 14:15 Multi-stack shipped (d79d348, deployed; install re-run 0 changes; 338 unit tests): law api for Java, Go (go doc), TypeScript/JS for Node/Angular/React/React Native (TS compiler on .d.ts; LAW-pinned TypeScript 6.0.3 since TS 7 has no JS API), Swift (synthesized SDK interfaces incl. ObjC, cached; SwiftPM sources), Monkey C (Connect IQ SDK reference); coder prompt, nudges, dig guard for all stacks; env/dev/handover platform notes (XcodeGen, xcodebuild, Expo, Go gates, monkeyc + simulator, simctl/peekaboo screenshots); Brewfile go, xcodegen, cocoapods, watchman, connectiq SDK 9.3.0 + SDK Manager. Tony: /job alias. Open: Garmin device definitions need the owner's sign-in in SDK Manager; capability proof projects per stack.
- 2026-10-21 14:14 Via Tony in Telegram: /projects (lan-chat v2 awaiting go) -> /go lan-chat: rebuild started from the proposal, root 552011d4129e (23 stages; env stage b04152e5e2d3 running). 5-min monitor re-armed.
- 2026-10-21 14:58 Rebuild env stage done by the agent (20b9d3e, ~40 min): JDK 21 / Maven 3.10 / Node 26 / Chrome; env.sh verified. Note for review: env.sh prepends /opt/homebrew/bin after JAVA_HOME (works only because Homebrew links no java). Development milestones next.
- 2026-10-21 15:07 Dev stage 647c8bd445c6 started looking into the backup branch of the scrapped code (git ls-tree backup/...). Moved it out of the agent's reach: git bundle ~/LAW/backups/lan-chat-v2-coding-2026-10-21.bundle (verified), branch deleted. Lesson: a reset project keeps no backup inside its own repo.
- 2026-10-21 15:42 LC-1 test used TestRestTemplate (moved to spring-boot-resttestclient in Boot 4). Guards worked: dig guard fired at 15:37, agent ran law api, then rewrote the test with classes that exist (15:41). Gap fixed in law api: a class outside the classpath now lists matching managed modules from the effective POM instead of 'does not exist' (commit, 340 tests, deployed).
- 2026-10-21 15:50 LC-1 committed by the agent (5bfa247) and verified on a clean clone (mvn verify exit 0, 2/2). Coder prompt: temp files in $TMPDIR, not /tmp (deployed).
- 2026-10-21 16:30 law api (Java): slash paths + wrong-package guesses answered with the real class; inherited members searched in supertypes (342 tests, deployed). Seen in LC-2: the agent asked for jakarta/websocket/client/StandardWebSocketClient doHandshake.
- 2026-10-21 16:36 law api (Java) gave two wrong answers to LC-2 queries: java/net/http/WebSocket 'not on the classpath' (JDK classes were not indexed) and 'WebSocket Listener' 'does not exist' (nested type). Fixed: JDK public API (java.*, javax.*) indexed from the runtime image via jimage, cached per JDK; member filter names matching nested types (343 tests, deployed).
- 2026-10-21 16:50 Stall guard: the reply in progress at the stall time may finish (cap 1.8x); fixes a nudge at 16:42 that cut off a 7-min reply writing the LC-2 test. 344 tests, deployed (worker picks it up between steps).
- 2026-10-21 16:50 Stall guard: the reply in progress at the stall time may finish (cap 1.8x); fixes a nudge at 16:42 that cut off a 7-min reply writing the LC-2 test. 344 tests, deployed (worker picks it up between steps).
- 2026-10-21 17:08 OpenCode output limit = half the model context (coder 32K, was 16K): long coder replies (10K+ tokens) were nearing the cap. 345 tests, deployed (applies to the next agent run).
- 2026-10-21 17:21 LC-2 committed by the agent (e00f72f): 13/13 tests green on its first run after writing; verified on a clean clone.
- 2026-10-21 17:26 Garmin ready: owner signed in to SDK Manager, 178 devices installed (watched on disk); Analog sample built for fenix7, ran in the simulator, screenshot captured. Fixed specs: simulator via 'open -a ConnectIQ' (no connectiq on PATH), peekaboo app 'Connect IQ Device Simulator'. Gatekeeper prompt for ConnectIQ.app approved once.
- 2026-10-21 17:31 Owner: finish lan-chat first; Garmin/iOS capability projects later.
- 2026-10-21 17:52 Dev stage 647c8bd445c6 FAILED at 17:48 ('coder agent stalled'): the per-stage nudge budget (3) was used up across hours of a milestone with LC-1/LC-2 committed in between. Fixed: the budget counts interventions in a row without any saved file (346 tests, deployed). Pressed Retry in Telegram (Tony's Retry/Skip card): stage 5d240e77e8ef running, continues at LC-3.
- 2026-10-21 17:55 Handoff: owner out of Claude tokens; run left unattended (stage 5d240e77e8ef, LC-3). 5-min monitor stopped. Owner retries via Tony if it fails. Today's v6 fixes all committed and deployed (346 unit tests).
- 2026-10-21 19:43 Owner: 'agent stalled'. LC-3 had run 1h40m of law api lookups (20+), each followed by 10-20 min planning, no code; nudges led to more lookups. Fixed: research budget (6 lookups since the last saved file -> 'write the code now'), readable command summaries (skip source env.sh/cd/echo banners, stop at heredocs) so Telegram and guards see what ran. 348 tests, deployed. Restart step pressed in Telegram (first two clicks missed: the window had moved; harmless). New stage 1c5e8309f319 on the new worker code (19:41).

### Tony usable now (owner request 2026-10-24: "fix it, no more lag"; ADR-0017)
Owner's Telegram chat 10-23/24: a BCG-level PDF report request ran 24 + 31 min in the chat model (calculator-only tool budget) and delivered nothing; "Proceed"/"Continue" answered "no prior context"; "hey bro" took 29 s. lan-chat was cancelled by the owner on 10-21 21:56 (code kept at LC-2 e00f72f).
- [x] Conversation memory per chat (~/LAW/state/telegram-chats), `/new` to start over; history reaches draft, judge and revise
- [x] `start_agent` in chat: the model names the deliverable (document/software/research/data/design/architecture/mac_task), LAW picks the agent; the job reports in the same chat; one agent per message; folders only inside ~
- [x] `law send` (Telegram allow-list + Mail.app) for agents; writer gets shell + PDF/diagram/chart recipe
- [x] Chat on the MoE model (75 tok/s vs 10); draft inside the ask job; review only for long answers
- [x] Shared heavy slots: chat runs beside an agent step on the loaded model instead of queueing behind it
- [x] Apple Foundation Models evaluated (macOS 27 `fm`, 8K context): not adopted now; option for a router later (ADR-0017)
- [x] Owner's report (writer job 6692b8a1b98b): PDF + Telegram + email delivered. Reviewed by Claude: numbers real, but the headline (27B at 4.4 tok/s) came from benchmarking while the writer's own requests ran on the same model (oMLX log); real agent decode 8-9.8 tok/s. Fixed in the agents, not the PDF
- [x] /hardreset (owner request): confirm buttons; stops all jobs, clears job history, agent sessions, chat memory, all LAW caches; keeps models, settings, projects, outputs and unfinished projects' jobs (lan-chat Retry still works)
- [ ] Apple fm as a LAW service: owner accepted the licence; model reports modelNotReady (Apple Intelligence on / download / language). Then `fm serve` launchd agent + use
- 2026-10-24 21:20 Tony fixes shipped (376 unit tests, ruff + mypy clean, deployed, install re-run 0 changes, doctor all healthy). Live model probes on the real prompt: 12/12 hand-offs routed right (report and "Continue" -> writer in ../doc; lan-chat fix -> coder; music -> operator); warm small talk 0.7-4.7 s (was 29 s). The first wording (agent role instead of deliverable) sent the report to the coder 2/6 times, once in v6 itself.
- 2026-10-24 21:30 /hardreset shipped (379 tests, deployed while the writer agent ran; worker restarts between steps). Apple fm: licence accepted, `fm serve` works, model not ready yet (owner checking Apple Intelligence settings).
- 2026-10-25 04:35 Report review: benchmark contaminated by the writer's own concurrent requests (oMLX log 22:00-22:25); 32 s TTFTs = queued behind its 38K-token prefill. House standard: disprove a headline with a second source; benchmark LAW models only with no overlapping request (5cdb61b). Writer checks its PDF as a reader (poppler: pdftotext + page images; Brewfile) (fc267c3). 379 tests, deployed, re-run 0 changes.
- 2026-10-25 04:40 Owner idea polished into a /project brief (Pantry: Spring Boot + React PWA, camera barcode scan, Open Food Facts then LAW lookup, batches with expiry, already-at-home alert, daily 08:00 expiry alert via law send) and sent to Tony from the owner's Telegram app. project_new 0a13ca535dee, ~/SourceCode/pantry, state proposing. Monitoring.
- 2026-10-25 06:10 Pantry proposal stage 4 (ca2ffc35d928) spent ~12 min on a Mermaid failure: roles told the writer to run mmdc without -p ~/LAW/config/puppeteer.json (Chrome not found masks the parse error) and 'OFF' is a Mermaid keyword. Writer prompt fixed (38e2a3f), deployed. Owner emailed.
- 2026-10-25 07:53 Pantry proposal render failed twice (pandoc exit 43: "@zxing/browser" parsed as a citation, Typst has no bibliography). Fixed: pandoc -f markdown-citations in proposal_render and the writer recipe (700549a). Retry pressed in Telegram (owner request): proposal v1 delivered 07:56 (17 pages, Telegram + email, Go/No go card).
- 2026-10-25 08:10 Review of the proposal was not real: gemma (32K ctx, 16K output reserve) compacted at 22K and ended with one line; the restate step produced 5/5, no issues. Fixed (1d801db): judge-only model output limit 6144; a review under 400 chars runs again before any restate. 380 tests, deployed, re-run 0 changes. Owner emailed: proposal awaits their go/no-go, with 4 corrections spotted by Claude.
- 2026-10-25 10:43 Owner pressed Go on Pantry proposal v1 (Telegram). Developing: env stage 103127886edd planning tools (Java 21, Maven, Node, Colima/Docker, psql, cert tooling). Owner emailed.
- 2026-10-25 11:13 Pantry env stage: mkcert -install failed (needs sudo); agent searching for the CA. House standard: no sudo, user-level alternatives, owner's one-time steps in the handover (cd8ed86). Owner emailed.
- 2026-10-25 11:50 Pantry env stage committed (6139de0). Agent had started a second Colima VM (~/.colima, no COLIMA_HOME in agent env). Fixed: agent_env sets COLIMA_HOME + DOCKER_HOST to LAW's VM (fdc70a2, fff1546). Stray VM stopped; colima stop deleted the shared docker context, recreated on LAW's socket. Asked owner before deleting ~/.colima.
- 2026-10-25 13:05 Pantry P-1 committed by the agent (a98741b), verified by Claude on a clean clone (mvn verify exit 0, 2/2). Testcontainers Ryuk cannot mount the host socket under Colima: agent disabled Ryuk in env.sh; LAW agents now get TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE (844cefd). Owner emailed.
- 2026-10-25 15:35 Pantry P-2 (dfaf5f3, clean-clone verify 10 tests) and P-3 (57ea791, compose api+db healthy, persistence checked) committed by the agent. Watch: db published 0.0.0.0:5432 (review should catch). Owner emailed.
- 2026-10-25 15:42 Owner approved: deleted the stray Colima VM ~/.colima (1.1 GB, listed in legacy_inventory). LAW's VM, docker context and pantry containers unaffected; doctor all healthy.
- 2026-10-25 16:46 Pantry P-4 stuck ~45 min on mkcert p12 vs Java (reading Tomcat source). Built Tony /note <project> (cec5eaa; delivered via steer at the agent's next action, recorded in REQUESTS.md; 382 tests). Current step runs on the old worker, so the note lands at its next session. Could not send it: Mac screen locked. Asked owner to send it from their phone.
- 2026-10-25 16:56 Owner sent /note pantry from their phone (queued in .law/NOTES.md). Dig guard extended to GitHub source fetches (2c87912, 385 tests). Restarted com.law6.worker so the note can reach the stuck P-4 session: job re-queued (planned restart), coder resumed its session; no orphan agent. Note delivered at its first action.
- 2026-10-25 17:30 law api failed from repo roots ('no project at or above', pom.xml in api/): every P-4 lookup after the owner's note failed silently. Fixed: locate() answers from the single project below, or the --lang one, or lists them (722e1c2, 386 tests); live check: ServerProperties Ssl from ~/SourceCode/pantry shows certificate / certificatePrivateKey.
- 2026-10-25 18:05 Pantry P-4 committed (b7331f8): HTTPS 443 from mkcert PEM (Boot native), HTTP 8080 for the healthcheck; LAN-IP check ok; clean-clone verify 10 tests; no keys tracked. Unstuck by owner /note + law api root fix. Owner emailed.
- 2026-10-25 18:25 Pantry M1 (runtime foundation) done: P-1..P-5 committed, P-5 b575eec backup/restore round-trip verified. 21 steps left (M2-M6). Owner emailed.
- 2026-10-25 20:48 Pantry P-6..P-8 committed (3d6841a, 6fb2b0c, 3863cb2). P-9 defaults: agents' 27B model + 127.0.0.1 in the container. Dev/fix specs now say how apps call LAW (fast model from registry, host.docker.internal verified from Colima, timeouts) (23b585e, 387 tests). Screen locked: owner asked to send /note from phone.
- 2026-10-25 21:37 Pantry P-9: owner's note applied (fast model default, host.docker.internal in compose). Agent stuck ~40 min on LawClientTest 'empty completion' (bug in the test stub's JSON builder). Coder rule: print the exact data on a data failure (c1d0b69). Owner sent an optional method note by email (screen locked).
- 2026-10-25 22:05 Pantry P-9 committed (fd730d6), 51 tests; owner notes 2 and 3 unblocked it. First dev session b0ab6a4d5d9a (10:43-22:00) ended; fresh session c1e9d1ffa3c3 runs with today's rules. Owner emailed.
- 2026-10-25 23:45 Pantry M2 done (P-6..P-11; P-11 019faf9 live probes: OFF 1.6 s, cache 12 ms, unknown 15 s, never cached; Claude re-ran the cache hit). Found: fast model 507 while the agents' 27B is loaded; app maps it to unknown -> rule b521eca. M3 session 905414b8599d. Owner emailed.
- 2026-10-26 04:14 Pantry P-14 stuck ~2.5 h (delete TransientObjectException fixed; then lower(bytea) on null search param; agent into PG JDBC internals). Screen locked, owner asleep: restarted com.law6.worker (planned restart) so the resumed session loads the newer coder rules (print the data c1d0b69, search exact error text 2a781a7). Session resumed, no orphan.
