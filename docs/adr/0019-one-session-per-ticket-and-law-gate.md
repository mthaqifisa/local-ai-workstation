# ADR-0019: One agent session per ticket, and LAW runs the gate after each
Status: Accepted (2026-10-26)

## Context
A build stage ran one OpenCode session for a whole milestone. In Pantry's M5 (frontend) that session ran for 10+
hours, its context passed 70K tokens and was compacted, and its tickets slowed down: P-19, P-20 and P-21 took two
hours or more each, and P-22 had saved nothing for 25 minutes when the stall guard fired. The oMLX log of the 27B
coder (2,157 requests) shows replies growing with the context. The median reply was 150 output tokens below a 15K
prompt and 702 above 70K. The 90th percentile went from 1.3K to 6.6K. Decode speed stayed flat (8-9 tok/s), so a
longer context means more reasoning per step, not faster work. Several tickets also bundled a whole screen (P-21:
banner, search, filters, cards and batch rows in one commit).

The owner also had to correct the coder by `/note` (Java rejecting the mkcert .p12, the wrong LAW address inside
Docker, a test stub hiding its data) after the build session had reported its own tests green. The owner wants
fewer of those: LAW itself should catch an unfinished ticket.

## Decision
- Each development stage still covers one milestone, but it runs one fresh agent session per ticket. LAW reads the
  first unticked ticket from `.law/PLAN.md` and names it in the prompt. The session does only that ticket and
  stops. Between sessions LAW drops the finished session from `.law/session.json`, so the next ticket starts fresh;
  an interrupted session is still resumed. What a later session needs travels in `.law/PROGRESS.md`.
- Tickets stay small: one endpoint, one part of a screen or one script, about two hours at most. The coder splits a
  bigger one into `<ID>.1`, `<ID>.2`, and the board reads those IDs.
- `.law/check.sh` is the project's whole gate (build, format and lint, every test; the platform gate of ADR-0016).
  The coder writes it with the first ticket and keeps it complete; it is committed with `.law/`. After every
  session LAW runs it itself, and checks that no product file was left uncommitted. Any failure, a missing script,
  or uncommitted files go to the next session, which fixes them first. The stage ends when its milestone is ticked
  and the gate passes, or after two sessions in a row that change nothing (as before: the next stage carries on).
- Not adopted, on the evidence: a cap on thinking tokens (Qwen's own results show accuracy rising with the thinking
  budget, and the owner wants top quality over speed); a "same failure three times" guard (Pantry's sessions had 58
  failing commands and only one real repeat); smaller or draft models.

## Consequences
- The coder's context stays near its start size, and every ticket ends with a gate that LAW ran, not the agent's
  report. That is the same principle as the review rounds: the agent never grades itself.
- Each ticket pays a short start-up (reading the brief, plan and progress) and one run of the full gate (minutes
  for a Spring Boot project with Testcontainers).
- A coder could weaken `check.sh`. The rules forbid it, and the code review rounds read the script with the rest
  of the code.
