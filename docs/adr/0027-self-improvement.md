# ADR-0027: Tony improves LAW through tested branches the owner applies

Status: accepted (owner request, 2026-11-23)

## Context
The owner wants Tony to take the initiative to improve itself, not wait to be told each fix. Earlier rules still
hold: only proven improvements, no scheduled or hidden jobs, nothing silent reaches the live system.

## Decision
- **Self-healing without approval:** a missing Python package in the run_python sandbox is installed, recorded in
  `~/LAW/state/calc-extra.txt` (reinstalled by the calc-env step) and reported in the chat.
- **Self-improvement with one approval:**
  - Sources: Tony calls `propose_improvement` when a missing skill, tool or rule made it fail, or the owner corrected
    the same thing twice; the owner can also send `/improve <what>`.
  - Build: Tony's watcher makes a git worktree of the repo on `improve/<id>` (in `~/SourceCode/OpenSourceLocalAI/v6-improve/`)
    and queues a coder agent. The agent follows CLAUDE.md and must pass ruff, mypy and the unit tests on that branch.
  - Offer: a built branch reaches the owner on Telegram with the agent's summary and real check output, and
    Apply / Reject buttons. A build with no commit is reported as not built.
  - Apply merges the branch, pushes and runs `law install`; Reject deletes the branch. Nothing is merged without
    Apply.
- Triggered only by events (a chat, a command), never on a schedule.

## Consequences
Tony can grow tools, rules and skills between owner sessions while every change stays reviewed, tested and
reversible with git.
