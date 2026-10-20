# ADR-0014: Project lifecycle with owner gates
Status: Accepted (2026-10-20)

## Context
The owner wants documentation before code and a say at each step: requirements → proposal → go/no go →
development → testing → verification → enhancements, repeated until they accept. A single long agent session
for a whole product overflowed the coder's context and lost its findings (lan-chat runs 5-8), and the tools a
stack needs (e.g. a JDK the libraries support) were not checked before coding.

## Decision
- `/code init` sends a template; the filled template (`/code new …`) starts a project. The record is
  `<project>/.law/project.json` (committed with the code); `~/LAW/state/projects.json` indexes names.
- States: proposing → awaiting_go → developing → awaiting_verification → done, with parked for a no go without
  notes. No go with notes, and Needs changes after a build, start the next proposal version, which keeps the
  document and adds "What changed in version N".
- Each phase is one flat pipeline ending in `project_gate`, which records the state and returns the question;
  Telegram shows it with buttons (callback `p|<action>|<project>`) and asks for notes when needed.
- Development = environment stage (the agent checks and installs the tools with Homebrew, writes `.law/env.sh`
  that LAW loads into every later session, and `.law/ENVIRONMENT.md`) → up to 12 milestone stages, one fresh
  agent session per unchecked milestone of this version in `.law/PLAN.md` (finished at once when all are ticked)
  → up to 3 review/fix rounds (skipped once a review passes) → verification gate.
- The project's `thinking` (low/medium/high) selects per-effort OpenCode configs for its agents.
- Push only on the owner's explicit Push button, and only to the `repo_url` they gave (credentials in URLs are
  rejected).

## Consequences
- Every decision is the owner's; nothing advances past a gate on its own.
- Fresh sessions per milestone keep each context small; progress lives on disk in `.law/`.
- Agents install software on the Mac (owner's decision: full access).
