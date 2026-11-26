# Style (LAW book kit)

Build and checks: `bash tools/build.sh` makes book.pdf and book.html from book.md and chapters/*.md; `bash .law/check.sh` builds
and runs tools/verify.py. Both are tested: write the book, do not rebuild the tooling. Change tools/ only when a
check is wrong, and say why in .law/PROGRESS.md.

- One source: book.md holds the metadata (and a preface, if any); each chapter is its own file in chapters/, named
  so they sort in order (`01-what-a-digital-bank-is.md`), starting with its `#` heading; sections are `##`.
  Never keep a second text. Footnote labels are shared by all chapters: prefix them (`[^ch1-bnm-dbf]`).
- Metadata in book.md's front matter: title, subtitle, date, edition, author, cover-image (a file in assets/).
- Citations: a footnote after the claim, `[^ch1-bnm-dbf]`, defined once in that chapter as
  `[^ch1-bnm-dbf]: Bank Negara Malaysia, *Licensing Framework for Digital Banks*, 31 Dec 2020, §5.2. https://...`
  (who, title, date or version, the clause, a link the reader can open). verify.py fails a footnote without a link.
- Facts come from .law/RESEARCH.md, where each one is recorded with its source, its saved copy in sources/ (not
  committed) and the exact quote or figure before it is written into the book. A fact that cannot be verified is
  not written. Search (`law search`) before calling a fact unknown, and never write about "the sources we hold":
  the reader cares what is published and by whom, not what this project downloaded.
- Each fact is stated once, where it belongs; later sections refer back to it instead of restating it.
- Diagrams: diagrams/<name>.mmd (Mermaid) or .dot (Graphviz); build.sh renders them. In the chapter:
  `![Figure caption that says what it shows](diagrams/<name>.png){#fig:<name>}`, referenced from the text as
  `@fig:<name>` (plain text, no backticks): LAW numbers it "Figure N" in both the PDF and the HTML. Never use raw
  Typst or HTML: it shows in one format only.
  verify.py fails a diagram whose text would be under 7 pt at page width or that is taller than a page:
  split it into parts (or lay it out top-to-bottom), never shrink it.
- Tables: Markdown pipe tables with a header row; keep them under about 6 columns so they fit A4.
- Code: fenced blocks with the language; lines at most 92 characters (verify.py checks).
- Images (cover art, photos): assets/<name>.png, with a caption.

## Readers and voice

Five people read every chapter, and each must finish it impressed:
- a bank's chief executive, who reads the opening and the summary boxes and must know what is decided and why;
- a senior banker, who checks that the banking, the risks and the regulation are right and stated precisely;
- a professor, who checks every claim against its source and every term against its definition;
- a seasoned consultant, who wants the decisions, the trade-offs and a recipe a team can follow on Monday;
- a fresh graduate, who has never built a bank and must be able to follow every step.

Write so all five are served at once:
- Each `##` section opens with an **In short** paragraph of two or three sentences: what the section decides and
  why it matters to the bank. Then the plain explanation with an everyday picture, then the technical depth, then
  the failure modes and the recipe.
- One idea per sentence. Sentences average under 20 words and never pass 40 (verify.py checks). Paragraphs of
  three to five sentences. Active voice: say who does what ("the ledger rejects the entry", not "the entry is
  rejected").
- Define every term, product and acronym where the reader first meets it in the book, in plain words: what it is
  and why it comes up here (search the book first; if it is defined earlier, point there).
- Dashes are rare (verify.py allows 8 per 1,000 words): use full stops, commas or brackets.
- Quote a source only for its key phrase (at most 30 words); paraphrase the rest and keep the footnote.
- Prefer a small table or a figure to a long paragraph of comparisons, numbers or steps.
- Each fact once. Do not restate what an earlier section said; refer back to it ("see 7.1").
- Numbers carry their unit and their meaning: "RM 100.00 available", not "100.00".
- Headings name the idea in plain words, numbered the way the book numbers them ("7.1 Double-entry design");
  the kit switches automatic numbering off when you number them yourself.
- Sequence diagrams: short participant names (under 20 characters), labels under 60 characters, and a step a
  participant does on its own is a `Note right of <who>: ...`, never a message to itself (its label covers the
  line). verify.py checks these.
- Before you finish a section, run `python3 tools/verify.py --all` and read what it says about your section.
