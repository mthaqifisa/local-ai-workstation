# ADR-0021: A pipeline adds a build stage while tickets are open
Status: Accepted (2026-10-28)

## Context
A project's pipeline is fixed when it starts: Pantry got 13 build stages, then review, fix and handover. Each build
stage has a 4 h budget (ADR-0019). Owner requests added tickets after the start (units, usage, barcode sources,
the design spec), and on 2026-10-28 stage 10 began with nine tickets open and three build stages left. The last
build stage would have ended on its budget and passed the project to review with tickets undone.

## Decision
- A build stage that ends with tickets still open returns `_again: true`. A stage that stops because sessions
  made no progress (IDLE_SESSIONS) does not, so a stuck build goes to review instead of looping.
- When the worker advances a pipeline past a stage with `_again`, and the next stage is not a copy of this one, it
  inserts a copy after it. A pipeline grows to at most `MAX_STAGES` (64) stages.
- `_again` is not carried to the next stage or into the pipeline's result.

## Consequences
- A pipeline cannot run out of build stages while work remains, and owner requests added mid-build are built.
- Stages already in the pipeline are used first; the copy is added only at the end of the build stages.
- The worker change applies to a running pipeline after the worker restarts.
