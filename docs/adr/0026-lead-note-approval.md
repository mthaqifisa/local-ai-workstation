# ADR-0026: Lead notes go to the owner for approval before the agent sees them

Date: 2026-11-23. Status: accepted (ENHANCE brief E8).

## Context

LAW's stall guards send generic nudges (build and fix, split the ticket, tick and commit). In October the build
session also wrote specific "lead notes" by hand for stuck coder sessions, and they unblocked tickets the generic nudges
did not (P-22.6, P-34.5, P-37). A replay on 2026-10-28 rebuilt 12 such moments and asked the 27B coder model, as lead
developer, for a note: it detected 8 of 9 stuck moments, and 4 of its 9 notes were as good as the hand-written ones
(1 partial, 3 missed the real blocker, 1 would have extended a polishing loop). Good enough to draft, not to send.

## Decision

- The second consecutive no-save stall in a coder ticket session (the moment the split nudge fires) also drafts a lead
  note in the background, with the replayed prompt (`law.lead.PROMPT`) and what the replay showed it needs: the
  ticket, the last 8 commits, LAW's last 80 action summaries and the project's files by size (for the 400-line rule).
  The generic nudge still goes out at once; the lead never delays or stops the agent, and its failure is only logged.
- A note becomes a draft in `~/LAW/data/lead/`. Tony sends it to the allow-listed owner with **Approve / Edit / Drop**.
  Approve sends the draft; Edit sends the owner's reply instead; Drop sends nothing. No answer within 60 min counts as
  Drop, because a stale note would describe a moment that has passed. One undecided draft per job at a time.
- An approved or edited note reaches the agent through the owner-note path (`ProjectMemory.add_note`), so it is
  delivered at the agent's next action and recorded in the project's REQUESTS.md like any owner note.
- Every decision is logged to `~/LAW/data/lead/outcomes.jsonl`. Every 10 decisions, the approval rate (approved +
  edited over all) is appended to `PROGRESS.md`, so the owner can decide later, on evidence, whether the step may run
  unattended.

## Consequences

- The owner gets at most one Telegram card per stuck ticket session, with the agent unchanged if they ignore it.
- The lead call shares the coder's model on oMLX (4-18 min per note in the replay); it runs only on the second stall.
- Rolling back is removing the `_draft_lead_note` call in `handlers/tasks.py`; the drafts folder can stay.

## Addendum 2026-11-26: no time limit

The owner could not answer a DBTC-12 draft in time and its buttons stopped working ("can we remove the default
60 minutes no response"). Drafts now wait for the owner's answer with no limit. An Approve or Edit that arrives after
the ticket is ticked in `.law/PLAN.md` is recorded as `outdated` and nothing reaches the agent, because the note was
about that ticket. One undecided draft per ticket (was per job), so an unanswered draft never blocks later tickets.
