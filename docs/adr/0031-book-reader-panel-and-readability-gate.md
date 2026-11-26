# ADR-0031: Books are written and checked for a five-reader panel, with a readability gate

Date: 2026-11-26. Status: accepted (owner request: "top banker, chief, professor, seasoned consultant and fresh
grads will read this book and I want everyone to feel impressed").

## Context
The tech-chapter trial (77 pages, Chapter 7) had verified facts and compiling, tested code, but its prose averaged
40 words a sentence (35% over 45 words) with 21 dashes per 1,000 words, headings read "1.1 7.1", two sequence
diagrams had labels over their lifelines and a clipped box, and nothing in LAW measured any of it.

## Decision
- The book kit's style guide names the five readers and what each needs; every `##` section opens with an
  "In short" paragraph for the executive, then the plain layer, the depth, failure modes and the recipe.
- tools/verify.py (rule 7) fails a sentence over 40 words, a section average over 22, more than 8 dashes per 1,000
  words, a quotation over 30 words, a sequence self-message and a label over 60 characters. The ticket gate checks
  the paragraphs and diagrams the change touches (`git diff HEAD`), so a ticket answers for its own writing and old
  text never blocks unrelated work; `--all` is the editor's whole-book view, summarised per section.
- The kit renders Mermaid with tools/mermaid.json (wrapped labels, wider boxes, no repeated bottom boxes) and turns
  automatic heading numbers off when the writer numbers headings.
- The editor's whole-book unit logs `verify.py --all` as findings; each section unit reads as the five readers; the
  reviewer's rubric scores against them.
- book_kit.upgrade brings running projects the kit's improvements at each build and editor stage, replacing only
  kit files no agent changed (recorded hashes, or a single git commit for older projects).

## Consequences
Fix rounds will rewrite much of the trial's existing prose (182 long sentences): slower this time, readable after.
Thresholds are deliberately those of clear professional writing, not of a children's book; revisit after the trial.

## Addendum 2026-11-26: readers per document, limits per document, roles per ticket (owner: "don't limit LAW")
- The reader panel is no longer the banking panel. Each plan names its document's readers (**Readers**: 3 to 5,
  from the brief); LAW copies them into .law/STYLE.md when the build starts (`book_kit.adopt_readers`). With no
  readers named, a neutral default applies: a decision-maker, a domain expert, a scholar, a practitioner and a
  newcomer. Writer, editor, reviewer and rubric all refer to "the readers .law/STYLE.md names".
- The readability limits are defaults; a document sets its own in book.md's front matter (`sentence-max`,
  `sentence-average`, `dashes-per-1000`, `quote-max`), e.g. a research paper.
- Any ticket can name its role, `[role <name>]` (a user guide in a software project for the writer, visuals for the
  designer, a data study for the analyst); the code-ticket rule for document projects is only the fallback.
