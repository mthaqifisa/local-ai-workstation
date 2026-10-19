# ADR-0006: Network exposure and agent access (user decision)
Status: Accepted (2026-10-19)

## Decision
- User-facing services bind `0.0.0.0` without login so the user's devices on the LAN can use them.
- Runtime agents run as the user with every permission allowed, including GUI control (ADR-0010).
- Exceptions, for function not restriction: Valkey binds loopback (internal plumbing, not a user service); oMLX binds loopback behind Caddy (ADR-0001); the Telegram bot answers only allow-listed user IDs because it is internet-reachable.

## Consequences (accepted by the user)
Anyone on the same network can use the AI and, through the agents, control the Mac. Documented in the threat model; `law doctor` prints the LAN URLs and current network name.
