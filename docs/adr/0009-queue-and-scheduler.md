# ADR-0009: Job queue and model-aware scheduler
Status: Accepted (2026-10-19)

## Context
v5 used a SQLite queue polled by one worker; no priorities across devices, no memory awareness beyond one model. Off-the-shelf job frameworks (Dramatiq 2.2, Taskiq 0.13, arq 0.28, Celery) provide durability and retries but cannot schedule by *which model a job needs* or *how much unified memory is free*, which is the core requirement on a 64 GB machine.

## Decision
- **Valkey 9** (BSD-3, Homebrew, loopback, AOF persistence in `~/LAW/data/valkey`) as the durable store.
- A small, fully tested LAW scheduler on top of Valkey primitives: jobs as hashes, a priority sorted set, per-job leases with heartbeats (crash recovery), retry with backoff, dead-letter set, Pub/Sub progress events.
- Scheduling policy: priority classes with aging; model affinity (prefer jobs for the already-loaded model); memory admission against the registry budget; light lane for small models runs concurrently.

## Consequences
~600 lines of scheduler code we own, justified by the missing capability; covered by unit + chaos tests (kill -9, restart, 30-job mixed load).
