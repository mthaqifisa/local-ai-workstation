# ADR-0022: Read-only Console for port forwarding

Status: accepted, 2026-11-01

## Context

The owner wants to see the Console from the internet through a VS Code port forward. The Console's API on 8100 has no
login (LAN decision, CLAUDE.md) and can cancel jobs, change settings and reset the machine. A static page alone cannot
work: it needs the API for its data.

## Decision

`law public` (service com.law6.public) serves the same `console.html` in read-only mode from a separate app that binds
to loopback on port 8110. Only 8110 is ever forwarded.

- Only GET routes exist: page, health, system, agents, jobs, boards. The app has no store and no write path.
- Each response is rebuilt from an allowlist of fields, so a new Console field stays private until it is listed.
- Chat text, prompts, ticket details, notes, logs, errors, agent actions and folders never leave: status, numbers and
  ticket titles do. Secrets, token-shaped text, email addresses and the home folder are redacted under that.
- Responses are cached for 3 s and API calls are capped, so request floods do not reach the API.

## Consequences

- The public page shows less than the LAN Console: no Services, Storage, Personas or Settings, and empty ticket logs.
- Access control is the tunnel's. VS Code's Private visibility needs the owner's GitHub sign-in; Public lets anyone with
  the link read statuses and ticket titles, so Private stays the default.
