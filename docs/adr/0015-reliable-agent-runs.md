# ADR-0015: Reliable agent runs on a slow local model
Status: Accepted (2026-10-20)

## Context
The first end-to-end runs of the project flow (LAN Chat) showed how a 27B model at ~9 tokens/s fails in practice:
findings lost to context compaction; a whole document attempted in one 16K-token generation; a stall guard that
could not tell a long generation from a loop; a writer re-researching on the web; pipelines that kept running on the
instructions stored when they were created; deploys that interrupted agents mid-step and used up their retries; and
no way to see from Telegram or the Console whether an agent was working, thinking or stuck.

## Decision
- Memory on disk: `.law/` (brief, requests, plan, progress, session); findings written as they go; large files in
  sections (at most ~1,500 words per write); LAW records a step in PROGRESS.md when the agent did not.
- Context 96K for Qwen3.8-27B (hybrid attention, ~64 KB/token): more room than 64K without the prefill spikes that
  reached oMLX's 48 GB ceiling at 98-103K. Correctness over speed (owner's rule).
- Stall guard: 25 minutes without a saved file -> resume the same session with a role-aware nudge; a silent step while
  the model is generating may run to 45 minutes.
- Named instructions (`spec_ref` + `spec_args`) resolved when a step runs.
- Library APIs (2026-10-21): the coder guessed Spring STOMP signatures from memory (`StompFrame`,
  `startSubscription`), and the compiler's "cannot find symbol" never shows the real API, so it dug through jars
  instead. `law api <Class> [member]` prints `javap -public` signatures from the project's resolved Maven test
  classpath, or the closest real class names when a class does not exist. The coder prompt and both nudges point to
  it; the library-dig guard also counts jar searches anywhere on disk and Maven local-repository lookups.
- Queue: planned restarts give back the attempt and requeue; a dead stage fails its pipeline; Retry (from the
  stopped step, earlier results kept), Skip and Restart via API, CLI, Console and Telegram buttons; every failure
  asks the owner Retry / Skip.
- Keep awake (2026-10-21): the worker holds `caffeinate -i -s -w <worker pid>` while any job runs; a coder step
  froze 31 minutes when the Mac idle-slept on battery (the only assertion had come from an interactive session).
- `law install` defers a worker restart while jobs run (the worker restarts itself between steps).
- Visibility: Console Agents tab and Telegram `/jobs [keyword]` with health (working, thinking, stalled, stuck,
  failed), step, last actions, to-do progress and model activity.
- Proposal depth `brief` (6 sections) or `full` (15); documents written at the writer's own thinking level.
- After development: a system handover document with real screenshots, rendered and emailed, before verification.
- Retention: finished jobs and agent conversations kept 14 days, editable in the Console (Storage), with per-cache
  sizes and Clear buttons.

## Consequences
More moving parts, each covered by unit tests; projects survive crashes, restarts and owner decisions days apart.
