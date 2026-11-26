# ADR-0032: A document's code lives in its own repo, by system; the document quotes it and links to it

Date: 2026-11-26. Status: accepted (owner), to build after the tech-chapter trial and before the full book.

## Context
In the tech-chapter trial the code sat inside the book folder (`~/RnD/<project>/code/`) and the chapter's listings
were pasted text, so a fix in the code left the book showing the old version. The repo's rule is that code lives in
`~/SourceCode`, and readers of a technical book want the complete, runnable system, not only excerpts.

## Decision (owner, 2026-11-26)
- One code repo per book: `~/SourceCode/<book>/`, organised by system, one folder per service
  (`services/ledger-service`, `services/ledger-client`, `services/eod-batch`), each named by what it does.
- One `compose.yaml` runs the whole system locally (database, every service) with health checks, and a `justfile`
  runs every build, test and lint (`just check`).
- The system runs only to verify the code: the book's gate runs `just check` and a short compose smoke test (start,
  call each health endpoint, stop), then everything is shut down. Nothing is left running.
- Code tickets work only in the code repo, as the coder; the writer writes the prose.
- The book keeps its code snippets, taken from the code repo files (whole files, line ranges or named regions), so
  every listing is tested code; each listing names its file.
- The book's last section says where the full code is: the GitHub repo's link.
- At the end, after a privacy scan and the owner's Go on Telegram, the repo is pushed to GitHub (private until the
  owner makes it public), and the finished PDF and HTML are added to it.

## Consequences
About 30-60 minutes more per book for the compose file and smoke test; Docker memory is used only during checks.
Acceptance test: migrate the tech-chapter trial's code into `~/SourceCode/digital-bank-tech-trial/` before the full
book starts.

## Scope
Any /rnd document whose subject includes working code (a book, a report, a research paper with its experiments, a
proposal with a prototype), not only books; "book" above stands for any of them.
