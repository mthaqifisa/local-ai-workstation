"""`/proposal`: a consultant-grade proposal, researched, written, independently reviewed, rendered and emailed.

A `proposal` job is a pipeline: the translator turns the request into a brief; the researcher agent searches the
web (through this Mac's SearXNG) and writes `research.md` with sources; the writer agent writes `proposal.md`
(Mermaid diagrams, SVG wireframes) to a fixed structure; the judge reviews it against a document rubric and the
writer fixes what it finds; `proposal_render` turns diagrams into images and builds the PDF with pandoc + Typst in
the house template; `proposal_mail` emails the PDF to the owner (LAW_REPORT_EMAIL) with the executive summary.

With a project folder (e.g. the working prototype) the files go in `<project>/docs/proposal/`; otherwise in
`~/LAW/outputs/docs/proposal-<time>-<topic>/`.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from law import mailer
from law.handlers.base import Runtime, run_cmd
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

RESEARCH_SPEC = """Your task in this stage is RESEARCH ONLY; do not write the proposal.
Search the web through this Mac's search engine: fetch {searx}/search?q=<query>&format=json and open the most
relevant results. Run several focused searches. If the folder holds a working prototype, read its README and
top-level structure (not the whole codebase) to understand what it already does.
Write {out}/research.md with: the market and user context; current alternatives and competitors (a table);
technology options with current versions; security, privacy and regulatory points; any sizing figures with how
they were derived. Every fact carries its source URL and access date; mark anything you could not verify."""

PROPOSAL_SPEC = """Write a consultant-grade proposal, to the standard of a top-tier strategy firm proposing a product
development to a client, in {out}/proposal.md (pandoc Markdown). Base facts on {out}/research.md; if the folder
holds a working prototype, present it as the prototype and describe only what its code really does.

Start the file with this YAML header (fill in the values):
---
title: <product name>
subtitle: <one-line value proposition>
prepared-for: <the client, from the request; otherwise "Client">
author: LAW
date: <today, e.g. 20 October 2026>
---

Then these sections, as level-1 headings in this order:
1. Executive summary: the recommendation first; the value, cost, timeline and the decision asked for (1 page).
2. Situation, complication, resolution.
3. Objectives and success measures (KPI table with targets).
4. Users and needs (personas, jobs to be done, pain points).
5. Market and alternatives (competitor table, differentiation), sourced.
6. Proposed solution (MVP scope vs later, feature table with MoSCoW priority).
7. The working prototype (what exists, what it proves, how to run it), or the proof-of-concept plan if none.
8. Technical design (stack table with rationale, data model, APIs, security, non-functional requirements).
9. Architecture (C4 context and container diagrams; deployment view).
10. User flows and system flows (flowchart and sequence diagrams for the key journeys).
11. Wireframes (the key screens).
12. Delivery plan (phases, a Gantt chart, team and governance).
13. Investment (effort and cost estimate with every assumption stated).
14. Risks and mitigations (table with likelihood, impact, owner).
15. Next steps and the decision required.
Appendix: sources (from research.md), assumptions, glossary.

Rules:
- Diagrams are Mermaid in fenced blocks written as ```{{.mermaid caption="<caption>"}}; keep them valid Mermaid.
- Wireframes are simple lo-fi SVG files you write in {out}/assets/ (greyscale; only rect, line, circle and text
  elements; viewBox 0 0 1200 800; no foreignObject), included as ![Wireframe: <screen>](assets/<file>.svg).
- Tables are pipe tables with a caption line `: <caption>` under them.
- Every claim is sourced or labelled as an assumption or estimate; numbers agree across sections.
- No placeholders, no TODOs, no lorem ipsum. Write the whole document."""

REVISE_SPEC = """This is version {version} of the proposal. The owner's feedback on the previous version:
{feedback}

Update {out}/proposal.md in place (keep the YAML header; set the subtitle to end with "(version {version})"):
keep what still holds, change what the feedback asks for, and add a level-1 section "What changed in version
{version}" right after the executive summary. If code already exists, read .law/PROGRESS.md and describe the
current state truthfully, and plan the changes as the next delivery phase. All other rules above still apply."""

DOC_RUBRIC = """You are reviewing a client proposal as a partner at a top-tier strategy firm. Score it 1-5:
5 = board-ready: answer-first, complete, sourced, consistent numbers, diagrams and wireframes present and correct;
4 = solid, cosmetic issues only; 3 = usable but has a real gap (missing section, unsourced claim, inconsistent
number, invalid diagram); 2 = significant gaps; 1 = unusable.
Read {out}/proposal.md, {out}/research.md and the files in {out}/assets/. Check every required section exists, facts
match research.md, numbers agree, Mermaid blocks are valid, wireframe SVGs exist, and claims about the prototype
match its code.
Reply with JSON only:
{{"score": <1-5>, "issues": ["<section or file: problem and fix>"], "summary": "<one sentence>"}}"""

MERMAID = re.compile(r"^```\s*(?:mermaid|\{\.mermaid([^}]*)\})\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
CAPTION = re.compile(r'caption\s*=\s*"([^"]*)"')
SUMMARY = re.compile(r"^#\s+Executive summary\s*$(.*?)(?=^#\s)", re.MULTILINE | re.DOTALL | re.IGNORECASE)
TITLE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "proposal"


def extract_diagrams(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace each Mermaid block with an image reference. Returns the new text and (caption, source) pairs."""
    diagrams: list[tuple[str, str]] = []

    def swap(match: re.Match[str]) -> str:
        n = len(diagrams) + 1
        cap = CAPTION.search(match.group(1) or "")
        caption = cap.group(1) if cap else f"Diagram {n}"
        diagrams.append((caption, match.group(2)))
        return f"![{caption}](assets/diagram-{n}.png){{width=100%}}\n"

    return MERMAID.sub(swap, markdown), diagrams


def executive_summary(markdown: str, limit: int = 2500) -> str:
    match = SUMMARY.search(markdown)
    text = (match.group(1) if match else markdown).strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


def proposal_stages(rt: Runtime, request: str, workdir: Path, out: str, *, version: int = 1, feedback: str = "",
                    effort: str | None = None, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The proposal pipeline. Version 1 researches first; later versions revise the existing document."""
    (workdir / out / "assets").mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {"prompt": request, "dir": str(workdir), "out": out, "version": version,
                            **({"effort": effort} if effort else {}), **(extra or {})}
    spec = PROPOSAL_SPEC.format(out=out)
    if version > 1:
        spec += "\n\n" + REVISE_SPEC.format(out=out, version=version, feedback=feedback or "(none given)")
    writer = {**base, "role": "writer", "spec": spec, "rubric": DOC_RUBRIC.format(out=out)}
    searx = f"http://127.0.0.1:{rt.reg.ports.searxng}"
    research = [{"kind": "task_work", "payload": {**base, "role": "researcher",
                                                  "spec": RESEARCH_SPEC.format(out=out, searx=searx)}}]
    return [
        {"kind": "task_translate", "payload": {**base, "role": "writer", "spec": spec}},
        *(research if version == 1 else []),
        {"kind": "task_work", "payload": writer},
        {"kind": "task_review", "payload": writer},
        {"kind": "task_fix", "payload": writer},
        {"kind": "proposal_render", "payload": base},
        {"kind": "proposal_mail", "payload": base},
    ]


def make_handlers(rt: Runtime) -> dict[str, Any]:
    def _folders(p: dict[str, Any]) -> tuple[Path, Path]:
        """(agent working folder, document folder)."""
        workdir = Path(p["dir"]).expanduser()
        return workdir, workdir / p["out"]

    async def proposal(ctx: Context) -> Outcome:
        p = ctx.job.payload
        request = str(p["prompt"]).strip()
        if p.get("dir"):
            workdir = Path(p["dir"]).expanduser()
            if not workdir.is_dir():
                raise Fatal(f"directory does not exist: {workdir}")
            out = "docs/proposal"
        else:
            workdir = rt.paths.outputs / "docs" / f"proposal-{time.strftime('%Y%m%d-%H%M')}-{slug(request)}"
            out = "."
        mailer.recipient(rt.paths.env_file)  # fail now, not after an hour of work, if no address is set
        return Stages(proposal_stages(rt, request, workdir, out))

    async def proposal_render(ctx: Context) -> Outcome:
        """Diagrams to PNG with mermaid-cli, then pandoc + Typst in the house template."""
        _, docs = _folders(ctx.job.payload)
        source = docs / "proposal.md"
        if not source.exists():
            raise Fatal(f"the writer did not produce {source}")
        text, diagrams = extract_diagrams(source.read_text())
        failed: list[str] = []
        config = rt.paths.config / "puppeteer.json"
        for n, (caption, code) in enumerate(diagrams, start=1):
            await ctx.progress(f"🖼 rendering diagram {n}/{len(diagrams)}: {caption}")
            mmd = docs / "assets" / f"diagram-{n}.mmd"
            mmd.write_text(code)
            cmd = ["mmdc", "-i", str(mmd), "-o", str(docs / "assets" / f"diagram-{n}.png"), "-s", "3", "-b", "white"]
            if config.exists():
                cmd[1:1] = ["-p", str(config)]
            try:
                await run_cmd(ctx, cmd, cwd=docs, timeout_s=180)
            except Fatal:
                failed.append(caption)
                text = text.replace(f"![{caption}](assets/diagram-{n}.png){{width=100%}}\n",
                                    f"```\n{code}```\n\n: {caption} (diagram source; it could not be drawn)\n")
        rendered = docs / ".proposal.render.md"
        rendered.write_text(text)
        version = int(ctx.job.payload.get("version", 1))
        pdf = docs / ("proposal.pdf" if version == 1 else f"proposal-v{version}.pdf")
        template = rt.paths.repo / "templates" / "docs" / "proposal.typst"
        await ctx.progress("📄 building the PDF")
        await run_cmd(ctx, ["pandoc", str(rendered), "-o", str(pdf), "--pdf-engine=typst",
                            f"--template={template}", f"--resource-path={docs}"], cwd=docs, timeout_s=600)
        rendered.unlink(missing_ok=True)
        if not pdf.exists() or pdf.stat().st_size < 10_000:
            raise Fatal("the PDF was not produced")
        return Done({"pdf": str(pdf), "diagrams": len(diagrams), "diagrams_failed": failed})

    async def proposal_mail(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = p.get("_carry") or {}
        _, docs = _folders(p)
        pdf = Path(carry["pdf"])
        markdown = (docs / "proposal.md").read_text()
        title_match = TITLE.search(markdown)
        title = title_match.group(1).strip().strip("'\"") if title_match else "Proposal"
        review = carry.get("review") or {}
        lines = [f"{title}: proposal attached (PDF).", "", "EXECUTIVE SUMMARY", "", executive_summary(markdown), ""]
        if review:
            lines.append(f"Independent review: {review.get('score')}/5"
                         + (" (revised after review)" if carry.get("fixed") else "") + f". {review.get('summary', '')}")
        if carry.get("diagrams_failed"):
            undrawn = ", ".join(carry["diagrams_failed"])
            lines.append(f"Diagrams shown as source because they could not be drawn: {undrawn}")
        lines += ["", f"Files on the Mac: {docs}", "Request: " + str(p["prompt"])[:500], "", "Sent by LAW"]
        to = mailer.recipient(rt.paths.env_file)
        version = int(p.get("version", 1))
        subject = f"Proposal: {title}" if version == 1 else f"Proposal v{version}: {title}"
        await ctx.progress(f"📧 emailing the proposal to {to}")
        try:
            mailer.send(to, subject, "\n".join(lines), [pdf], rt.paths.cache / "tmp")
        except mailer.MailError as exc:
            raise Fatal(f"{exc} (the PDF is at {pdf})") from exc
        return Done({"pdf": str(pdf), "emailed": to,
                     "answer": f"📧 **{title}**: proposal emailed to {to}.\n\nPDF: `{pdf}`"})

    return {"proposal": proposal, "proposal_render": proposal_render, "proposal_mail": proposal_mail}
