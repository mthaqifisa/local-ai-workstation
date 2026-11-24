# ADR-0029: Agent productivity: book kit, ticket context, lessons, research first
Status: Accepted (2026-11-24)

## Context
Owner: improve how the agents work, faster, with quality over speed. Measured first: 8 h of recent agent time
(17 jobs) went 27 % to model thinking, 22 % to mixed shell work, 16 % to probing tools in scratch folders, 11 % to
builds, 10 % to re-reading LAW's own `.law/` files at every ticket start. The first /rnd trial spent 99 + 40 min
building a pandoc/Typst template and 0 min on research before it was stopped.

## Decisions
1. **Book kit** (`templates/rnd`, `law/book_kit.py`): every document project starts with a tested build (book.md +
   chapters/*.md -> A4 PDF and one-file HTML, Mermaid/Graphviz diagrams) and a gate that lists every problem:
   diagram text under 7 pt at page width or taller than a page (Mermaid measured from its SVG, because mmdc
   shrinks wide PNGs to 800 px and hides it), citations without a link, duplicate labels, missing images, code
   lines too long for A4. The environment stage only runs it. The planner gets its style guide.
2. **Research before writing**: plans and tickets start each chapter with research recorded in `.law/RESEARCH.md`
   (source, clause, link, exact quote); writing uses only recorded facts. Faster to write, and the editor checks
   against it.
3. **Ticket context**: each ticket session gets LAW's copy of the brief, its milestone, the two latest progress
   entries and the style guide, instead of opening those files again.
4. **Lessons**: an agent records a problem other projects will hit in `.law/LESSONS.md`; LAW keeps it only after
   that ticket's gate passed, in `~/LAW/data/lessons/<role>.md` (machine-local), and gives later sessions of the
   role the newest 3,000 characters.
5. **Estimates**: tickets carry `[est …]`; LAW calibrates them by the project's real pace
   (`law/estimate.py`) for the Console's countdowns.

## Results (ch.1 trial, same request)
| | Before | After |
|---|---|---|
| Tickets | 22, incl. 4 "integrate and rebuild" and tooling | 16, research first |
| Environment stage | 12 min | 7 min |
| Template/tooling work | 139 min | 0 |
| First research | ~2 h in | 9 min in |
Quality gates are unchanged or stricter (the kit's checks are new and deterministic). Remaining measurements
(per-ticket time, editor findings) are added when the trial finishes.

## Addendum 2026-11-24: research that tries to get around websites

DBCT-4 (PIDM deposit insurance) ran 76 minutes without saving anything: from minute 20 it tried to decrypt the AGC law
database's search API, then searched web archives and BNM sitemaps, even though the Act and the 2016 order were already
in `sources/`. The two stall nudges did not stop it; the lead's note, approved by the owner, did. LAW now spots these
commands (`WEB_DIG`: crypto in a fetch script, web.archive.org and its cdx API, sitemap crawls, faked referers) for
writers. Every third one, it resumes the session with `WEB_DIG_NUDGE`: work from `sources/` and pages that open
normally, and write anything missing as "owner to confirm". This is guidance, not a strike. A plain fetch, even with a
browser user agent, does not count. Replayed on today's log, it would have stepped in at 12:07 instead of the owner's
approval at 12:48. DBCT-1 to 3 had no such commands. `DOC_PROBE_NUDGE` now gets its own handling and progress line;
before, it was reported as "reading library internals".
