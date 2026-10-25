# ADR-0018: Black-box QA in every review round, and `/bugfix`
Status: Accepted (2026-10-26)

## Context
After the milestones, a project went through up to three review/fix rounds in which an independent reviewer read
the code and ran its tests. Nobody used the product the way the owner would. The agent that writes a feature also
writes its tests, so they share blind spots: in Pantry, P-12's product search crashed on an empty search term and
its own tests never tried one; it surfaced only by chance in P-14's tests. The owner also had no way to report a
bug on the fly.

## Decision
- A `qa` role tests the product, never the code. It runs inside each project review round (`task_review` with the
  `project.review` rubric), before the code review, as a fresh session every round. It uses the judge model:
  another model family than the coder, and the model the review stage already holds (no second large model next
  to it: the 48 GB ceiling refuses that with HTTP 507).
- QA starts the product as the README says and checks every requirement and acceptance criterion through the
  product's own interfaces (headless browser, HTTP API, commands), then the edges a user hits. A guard stops it when
  it reads or searches source files (outside its own scratch scripts and `.law/`), three times at most without a
  strike.
- `.law/BUGS.md` is the single bug list (`## BUG-n · status · reporter · time`). QA files bugs and retests them
  (`verified` or `reopened`); the coder fixes them, adds a test, and marks them `fixed`; only QA closes a bug. A
  round passes only when no bug is open, fixed-but-unretested or reopened, and the code review passes.
- `/bugfix <project> <what goes wrong>` in Telegram adds an owner bug. While a build of the project runs, the
  working agent gets it as a note (fix after the current step) and the next QA round retests it. Otherwise a
  `project_bugfix` pipeline runs: fix, then QA retest plus code review, up to three times, then a report in the chat.
- The Console board shows a ticket per bug (from `BUGS.md`) and the fix and retest stages of each `/bugfix`.

## Consequences
- Pantry and every running project get QA without changing their pipelines: their review rounds already exist.
- A review round takes longer (the product is started and exercised), and a project cannot reach the owner's
  verification with a bug QA can reproduce.
- QA's independence rests on a different model, a fresh session and the guard; it still reads the README and the
  brief, which the developer wrote from, so a requirement missing from the brief is missed by both.
