# ADR-0013: Translated briefs, per-project memory and the proposal pipeline
Status: Accepted (2026-10-20)

## Context
- Vague requests produced vague work, and the coder had no written acceptance criteria to work towards.
- Agent sessions die (worker restarts, crashes, context compaction). The lan-chat runs showed an agent that
  forgot what it had read and started over; nothing on disk said what was done or what came next.
- `/code` without a folder told the agent to create files directly in `~/SourceCode`, so separate requests
  ended up in one place even when the user named the project.
- The owner wants documentation before code: a consultant-grade proposal on request, delivered by email.

## Decision
- **Translator role** (Qwen3.8-27B, the coder's model, so no swap): every reviewed agent task runs
  translate → work → review → fix. The brief keeps everything the user said, adds acceptance criteria and gates,
  and labels each default as an assumption. For a new coding project it names the folder; LAW validates the
  name (directly under `~/SourceCode`, kebab-case) and creates it. A folder is reused only when the request
  names that project.
- **`<project>/.law/`**, modelled on `.claude/`: LAW writes `PROMPT.md` and `REQUESTS.md`; the agent maintains
  `RESEARCH.md`, `PLAN.md` and `PROGRESS.md`. `session.json` (git-ignored) maps a job to its OpenCode session,
  so a requeued job resumes the same session with "continue from PROGRESS.md".
- **`/proposal`**: translate → research agent (web search through SearXNG's JSON API, sources with dates) →
  writer to a fixed 15-section structure → document-rubric review by the judge → fix → render (Mermaid to PNG
  with mermaid-cli; pandoc + Typst with the house template) → Mail.app sends the PDF to `LAW_REPORT_EMAIL`.
  The address lives in `~/LAW/state/.env`, is asked by the installer and is editable in the Console.
- Mermaid diagrams are drawn as PNG because Typst cannot render the HTML labels inside Mermaid's SVG output.
  A diagram that fails to draw stays in the PDF as source and is named in the email, rather than failing the job
  after an hour of writing.

## Consequences
- One extra model call (a few minutes) before every reviewed task; no model swap.
- Projects carry their own history; commit `.law/` with the code.
- Sending mail needs macOS Automation permission for Mail the first time (System Settings → Privacy & Security →
  Automation). Until it is granted, the job fails with that instruction and the PDF path.
- mermaid-cli needs a headless browser: the `doc-tools` install step fetches the exact chrome-headless-shell
  version its puppeteer pins (about 100 MB) into `~/LAW/runtime/chrome`.
