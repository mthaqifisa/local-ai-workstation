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

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from law import mailer, specs
from law.handlers.base import Runtime, run_cmd
from law.handlers.core import answer
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

RESEARCH_SPEC = """Your task in this stage is RESEARCH ONLY; do not write the proposal.
Search the web through this Mac's search engine: fetch {searx}/search?q=<query>&format=json and open the most
relevant results. Run several focused searches. If the folder holds a working prototype, read its README and
top-level structure (not the whole codebase) to understand what it already does.
Write {out}/research.md AS YOU GO: create it after your first search and append each topic's findings with their
sources as soon as you have them, so nothing is lost if your session is interrupted. It must end up with: the
market and user context; current alternatives and competitors (a table);
technology options with current versions; security, privacy and regulatory points; any sizing figures with how
they were derived. Every fact carries its source URL and access date; mark anything you could not verify."""

PROPOSAL_SPEC = """Write a {kind}, to the standard of a top-tier strategy firm proposing a product development to a
client, in {out}/proposal.md (pandoc Markdown). Base facts on {out}/research.md; if the folder holds a working
prototype, present it as the prototype and describe only what its code really does.
Do not browse the web in this stage: the research is done and research.md is your source. A fact it lacks is
written as an assumption or estimate, never looked up.

Start the file with this YAML header (fill in the values):
---
title: <product name>
subtitle: <one-line value proposition>
prepared-for: <the client, from the request; otherwise "Client">
author: LAW
date: <today, e.g. 20 October 2026>
---

Then these sections, as level-1 headings in this order:
{sections}

Rules:
- Diagrams are Mermaid in fenced blocks written as ```{{.mermaid caption="<caption>"}}; keep them valid Mermaid.
- Wireframes are simple lo-fi SVG files you write in {out}/assets/ (greyscale; only rect, line, circle and text
  elements; viewBox 0 0 1200 800; no foreignObject), included as ![Wireframe: <screen>](assets/<file>.svg).
- Tables are pipe tables with a caption line `: <caption>` under them.
- Every claim is sourced or labelled as an assumption or estimate; numbers agree across sections.
- No placeholders, no TODOs, no lorem ipsum. Write the whole document{length}.
- First list the sections as a checklist in .law/PLAN.md under a heading "## Proposal v<version> sections".
  Create proposal.md with the YAML header and section 1, then add one section per edit (at most about 1,500
  words per write), ticking each in PLAN.md as you go."""

# depth "full": the consultant-grade document; "brief": the same decisions in about a third of the length, for
# small products (chosen in the /code init template; /proposal is always full).
SECTION_LISTS: dict[str, list[tuple[str, str]]] = {
    "full": [
        ("Executive summary", "the recommendation first; the value, cost, timeline and the decision asked for "
                              "(1 page)"),
        ("Situation, complication, resolution", "the situation, what makes it a problem now, and the resolution"),
        ("Objectives and success measures", "a KPI table with targets"),
        ("Users and needs", "personas, jobs to be done, pain points"),
        ("Market and alternatives", "a competitor table and the differentiation, sourced"),
        ("Proposed solution", "MVP scope vs later, a feature table with MoSCoW priority"),
        ("The working prototype", "what exists, what it proves, how to run it; or the proof-of-concept plan if none"),
        ("Technical design", "a stack table with rationale, the data model, APIs, security, non-functional "
                             "requirements"),
        ("Architecture", "C4 context and container diagrams; the deployment view"),
        ("User flows and system flows", "flowchart and sequence diagrams for the key journeys"),
        ("Wireframes", "the key screens"),
        ("Delivery plan", "phases, a Gantt chart, team and governance"),
        ("Investment", "the effort and cost estimate with every assumption stated"),
        ("Risks and mitigations", "a table with likelihood, impact, owner"),
        ("Next steps", "the next steps and the decision required"),
        ("Appendix", "sources (from research.md), assumptions, glossary"),
    ],
    "brief": [
        ("Executive summary", "the recommendation, the value, the effort and the decision asked for"),
        ("Problem, users and alternatives", "the problem, the users, and a short sourced competitor table"),
        ("Solution and technical design", "MVP features with MoSCoW priority; a stack table with rationale, the "
                                          "data model, APIs, security"),
        ("Architecture and flows", "one container diagram, one user flow and one sequence diagram"),
        ("Wireframes", "the two or three key screens"),
        ("Delivery plan, effort and risks", "milestones, an effort estimate with assumptions, a risk table"),
        ("Appendix", "sources and assumptions"),
    ],
}
SECTIONS = {depth: "\n".join(f"{i}. {h}: {d}." if h != "Appendix" else f"Appendix: {d}."
                             for i, (h, d) in enumerate(items, start=1))
            for depth, items in SECTION_LISTS.items()}
LENGTH = {"full": "", "brief": " in about 2,500 to 3,500 words"}
KINDS = {"full": "consultant-grade proposal", "brief": "concise proposal brief"}

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

REPAIR_TRIES = 3  # diagram repairs before a broken diagram goes to the review
MERMAID = re.compile(r"^```\s*(?:mermaid|\{\.mermaid([^}]*)\})\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
CAPTION = re.compile(r'caption\s*=\s*"([^"]*)"')
# Documents this module renders and emails: the file stem, its label, and the section quoted in the email.
DOCS = {"proposal": {"label": "Proposal", "summary": "Executive summary"},
        "handover": {"label": "Handover", "summary": "Overview"}}
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


def mermaid_error(output: str) -> str:
    """The useful part of mmdc's failure: 'Parse error on line N:' with the offending line and what it expected
    (it sits above a long stack trace)."""
    lines = [ln.rstrip() for ln in output.splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if "Parse error" in ln or "Lexical error" in ln), None)
    if start is None:
        start = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("Error")), None)
    if start is None:
        return "Mermaid could not draw it"
    picked = []
    for ln in lines[start:start + 6]:
        if ln.lstrip().startswith("at ") or "Parser." in ln:
            break
        picked.append(ln.strip())
    return " | ".join(picked)[:400]


def executive_summary(markdown: str, limit: int = 2500, heading: str = "Executive summary") -> str:
    pattern = rf"^#\s+(?:\d+\.?\s*)?{re.escape(heading)}\s*$(.*?)(?=^#\s)"
    match = re.search(pattern, markdown, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    text = (match.group(1) if match else markdown).strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


@specs.register("proposal.research")
def research_spec(a: dict[str, Any]) -> str:
    text = RESEARCH_SPEC.format(out=a["out"], searx=a["searx"])
    if a.get("depth") == "brief":
        text += ("\nThis is for a brief proposal: keep it focused, at most about eight searches and 1,500 words, "
                 "covering the alternatives, the technology versions and the main risks.")
    return text


@specs.register("proposal.write")
def write_spec(a: dict[str, Any]) -> str:
    depth = a.get("depth", "full")
    text = PROPOSAL_SPEC.format(out=a["out"], kind=KINDS[depth], sections=SECTIONS[depth], length=LENGTH[depth])
    if int(a.get("version", 1)) > 1:
        text += "\n\n" + REVISE_SPEC.format(out=a["out"], version=a["version"],
                                             feedback=a.get("feedback") or "(none given)")
    return text


SECTION_SPEC = """You are writing ONE section of a {kind} in {out}/proposal.md: section {k} of {n}, "{heading}".
{action}
This section covers: {description}
Base every fact on {out}/research.md and stay consistent with the sections already in proposal.md. Do not browse
the web; a fact research.md lacks is written as an assumption or estimate.
Format rules:
- Diagrams are Mermaid in fenced blocks written as ```{{.mermaid caption="<caption>"}}; keep them valid Mermaid.
- Wireframes are simple lo-fi SVG files you write in {out}/assets/ (greyscale; only rect, line, circle and text
  elements; viewBox 0 0 1200 800; no foreignObject), included as ![Wireframe: <screen>](assets/<file>.svg).
- Tables are pipe tables with a caption line `: <caption>` under them.
- Every claim is sourced or labelled as an assumption or estimate. No placeholders or TODOs.
Write only this section (about {words} words at most), as the level-1 heading "# {heading}" and its content, then
stop. Do not change other sections."""

CREATE = """Create the file: start it with this YAML header (fill in the values), then the section:
---
title: <product name>
subtitle: <one-line value proposition>
prepared-for: <the client, from the request; otherwise "Client">
author: LAW
date: <today, e.g. 20 October 2026>
---"""
APPEND = "Append the section to the end of the existing file; do not rewrite what is there."
REVISE_ONE = ("This is version {version}. The owner's feedback on the previous version:\n{feedback}\n"
              "Revise this section in place so it reflects the feedback (keep what still holds); if the section is "
              "missing, add it in its place in the order of sections. Do not touch other sections.")
WHAT_CHANGED = ("What changed in version {version}: the owner's feedback, each change made in response and where, "
                "and anything not changed with the reason. Place it right after the executive summary.")


def section_list(depth: str) -> list[tuple[str, str]]:
    """(heading, what it covers) for each section of a depth."""
    return list(SECTION_LISTS[depth])


@specs.register("proposal.section")
def section_spec(a: dict[str, Any]) -> str:
    depth = a.get("depth", "full")
    version, k = int(a.get("version", 1)), int(a["k"])
    if version > 1:
        action = REVISE_ONE.format(version=version, feedback=a.get("feedback") or "(none given)")
    else:
        action = CREATE if k == 1 else APPEND
    return SECTION_SPEC.format(kind=KINDS[depth], out=a["out"], k=k, n=a["n"], heading=a["heading"], action=action,
                               description=a["description"], words=900 if depth == "full" else 600)


@specs.register("proposal.fixrules")
def fix_rules(a: dict[str, Any]) -> str:
    return ("Fix only what the issues name, editing those sections in place in {out}/proposal.md, one section per "
            "edit. Keep the YAML header and the other sections. Diagrams stay valid Mermaid in "
            "```{{.mermaid caption=...}} blocks; wireframes are SVG files in {out}/assets/; facts come from "
            "research.md. Do not browse the web.").format(out=a["out"])


def section_written(markdown: str, heading: str) -> bool:
    return bool(re.search(rf"^#\s+(?:\d+\.?\s*)?{re.escape(heading)}\b", markdown, re.MULTILINE | re.IGNORECASE))


@specs.register("proposal.rubric")
def doc_rubric(a: dict[str, Any]) -> str:
    return DOC_RUBRIC.format(out=a["out"])


def section_stages(base: dict[str, Any], args: dict[str, Any], version: int) -> list[dict[str, Any]]:
    """One short writer session per section, so no single step can outgrow the output limit, and a crash costs one
    section at most. A section already in proposal.md is skipped (version 1), so a retried pipeline resumes."""
    sections = section_list(str(args.get("depth", "full")))
    if version > 1:
        sections.insert(1, (f"What changed in version {version}", WHAT_CHANGED.format(version=version)))
    n = len(sections)
    # Thinking off: the section's content and facts are given (research.md, the brief); measured 6-9x faster for
    # prose, and the independent review (with thinking) still checks the document.
    return [{"kind": "task_work", "payload": {
        **base, "role": "writer", "spec_ref": "proposal.section", "effort": "off",
        "section": {"file": f"{args['out']}/proposal.md", "heading": heading, "skip_if_written": version == 1},
        "spec_args": {**args, "k": k, "n": n, "heading": heading, "description": description}}}
        for k, (heading, description) in enumerate(sections, start=1)]


def proposal_stages(rt: Runtime, request: str, workdir: Path, out: str, *, version: int = 1, feedback: str = "",
                    depth: str = "full", effort: str | None = None,
                    extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The proposal pipeline. Version 1 researches first; later versions revise the existing document.
    Instructions are named (spec_ref) and built when each step runs, so they are always the current ones."""
    (workdir / out / "assets").mkdir(parents=True, exist_ok=True)
    args = {"out": out, "version": version, "feedback": feedback, "depth": depth,
            "searx": f"http://127.0.0.1:{rt.reg.ports.searxng}"}
    base: dict[str, Any] = {"prompt": request, "dir": str(workdir), "out": out, "version": version,
                            **({"effort": effort} if effort else {}), **(extra or {})}
    research = [{"kind": "task_work", "payload": {**base, "role": "researcher", "spec_ref": "proposal.research",
                                                  "spec_args": args}}]
    return [
        {"kind": "task_translate", "payload": {**base, "role": "writer", "spec_ref": "proposal.write",
                                               "spec_args": args}},
        *(research if version == 1 else []),
        *section_stages(base, args, version),
        {"kind": "proposal_check", "payload": base},
        {"kind": "task_review", "payload": {**base, "role": "writer", "spec_ref": "proposal.fixrules",
                                            "rubric_ref": "proposal.rubric", "spec_args": args}},
        {"kind": "task_fix", "payload": {**base, "role": "writer", "spec_ref": "proposal.fixrules",
                                         "rubric_ref": "proposal.rubric", "spec_args": args}},
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

    async def _draw(ctx: Context, docs: Path, n: int, caption: str, code: str) -> str | None:
        """Draw one Mermaid diagram to assets/diagram-<n>.png. Returns the parse error, or None when it drew."""
        mmd = docs / "assets" / f"diagram-{n}.mmd"
        mmd.parent.mkdir(parents=True, exist_ok=True)
        mmd.write_text(code)
        cmd = ["mmdc", "-i", str(mmd), "-o", str(docs / "assets" / f"diagram-{n}.png"), "-s", "3", "-b", "white"]
        config = rt.paths.config / "puppeteer.json"
        if config.exists():
            cmd[1:1] = ["-p", str(config)]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=docs, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        except TimeoutError:
            proc.kill()
            return "Mermaid timed out drawing it"
        if proc.returncode == 0:
            return None
        return mermaid_error(out.decode(errors="replace"))

    async def proposal_check(ctx: Context) -> Outcome:
        """Before the review: draw every diagram. The reviewer reads text and cannot tell that a diagram will not
        draw (seen: invalid Mermaid passed a 5/5 review), so failures go to the review as issues to fix."""
        _, docs = _folders(ctx.job.payload)
        source = docs / f"{ctx.job.payload.get('doc', 'proposal')}.md"
        if not source.exists():
            raise Fatal(f"the agent did not produce {source}")
        _, diagrams = extract_diagrams(source.read_text())
        errors, repaired = [], 0
        for n, (caption, code) in enumerate(diagrams, start=1):
            await ctx.progress(f"🖼 checking diagram {n}/{len(diagrams)}: {caption}")
            error = await _draw(ctx, docs, n, caption, code)
            original, attempt = code, 0
            while error and attempt < REPAIR_TRIES:  # repair it here: short, verified by drawing it again
                attempt += 1
                await ctx.progress(f"🛠 repairing diagram {n} (try {attempt}): {error[:80]}")
                fixed = await _repair(ctx, code, error)
                if not fixed:
                    continue
                code = fixed  # the next try improves on this attempt
                error = await _draw(ctx, docs, n, caption, fixed)
            if code is not original and not error:
                text = source.read_text()  # what is in the file is the original, whichever try worked
                if original in text:
                    source.write_text(text.replace(original, code, 1))
                    repaired += 1
                else:
                    error = "repaired, but the original diagram was not found in the document to replace"
            if error:
                errors.append(f"Diagram \"{caption}\" does not draw: {error}. Fix its Mermaid syntax.")
        return Done({"diagram_errors": errors, "diagrams_repaired": repaired})

    async def _repair(ctx: Context, code: str, error: str) -> str | None:
        """Ask the writer's model for a corrected diagram (just the Mermaid, no prose)."""
        prompt = (f"This Mermaid diagram does not render. Error from mermaid-cli:\n{error}\n\nDiagram:\n{code}\n"
                  "Return the corrected diagram only, as Mermaid inside one ```mermaid block. Keep its meaning, nodes "
                  "and labels; change only what is needed so it renders (one node per line, quote labels that "
                  "contain punctuation, valid arrows).")
        reply = await answer(rt, "writer", [{"role": "user", "content": prompt}], ctx)
        m = re.search(r"```(?:mermaid)?\s*\n(.*?)```", reply, re.DOTALL)
        fixed = (m.group(1) if m else reply).strip()
        return fixed + "\n" if fixed and fixed != code.strip() else None

    async def proposal_render(ctx: Context) -> Outcome:
        """Diagrams to PNG with mermaid-cli, then pandoc + Typst in the house template."""
        _, docs = _folders(ctx.job.payload)
        doc = str(ctx.job.payload.get("doc", "proposal"))
        source = docs / f"{doc}.md"
        if not source.exists():
            raise Fatal(f"the agent did not produce {source}")
        text, diagrams = extract_diagrams(source.read_text())
        failed: list[str] = []
        for n, (caption, code) in enumerate(diagrams, start=1):
            await ctx.progress(f"🖼 rendering diagram {n}/{len(diagrams)}: {caption}")
            if await _draw(ctx, docs, n, caption, code):
                failed.append(caption)
                text = text.replace(f"![{caption}](assets/diagram-{n}.png){{width=100%}}\n",
                                    f"```\n{code}```\n\n: {caption} (diagram source; it could not be drawn)\n")
        rendered = docs / f".{doc}.render.md"
        rendered.write_text(text)
        version = int(ctx.job.payload.get("version", 1))
        pdf = docs / (f"{doc}.pdf" if version == 1 else f"{doc}-v{version}.pdf")
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
        doc = str(p.get("doc", "proposal"))
        info = DOCS[doc]
        markdown = (docs / f"{doc}.md").read_text()
        title_match = TITLE.search(markdown)
        title = title_match.group(1).strip().strip("'\"") if title_match else info["label"]
        review = carry.get("review") or {}
        lines = [f"{title}: {info['label'].lower()} attached (PDF).", "", info["summary"].upper(), "",
                 executive_summary(markdown, heading=info["summary"]), ""]
        if review:
            lines.append(f"Independent review: {review.get('score')}/5"
                         + (" (revised after review)" if carry.get("fixed") else "") + f". {review.get('summary', '')}")
        if carry.get("diagrams_failed"):
            undrawn = ", ".join(carry["diagrams_failed"])
            lines.append(f"Diagrams shown as source because they could not be drawn: {undrawn}")
        lines += ["", f"Files on the Mac: {docs}", "Request: " + str(p["prompt"])[:500], "", "Sent by LAW"]
        version = int(p.get("version", 1))
        label = info["label"]
        subject = f"{label}: {title}" if version == 1 else f"{label} v{version}: {title}"
        # Email is a second channel: if it fails (no address yet, or macOS has not allowed LAW to control Mail, which
        # needs one click at an unlocked Mac), the document still reaches the owner in Telegram and the job goes on.
        try:
            to = mailer.recipient(rt.paths.env_file)
            await ctx.progress(f"📧 emailing the {label.lower()} to {to}")
            mailer.send(to, subject, "\n".join(lines), [pdf], rt.paths.cache / "tmp")
        except mailer.MailError as exc:
            await ctx.progress(f"⚠️ email not sent: {exc}")
            return Done({"pdf": str(pdf), "emailed": None, "mail_error": str(exc),
                         "answer": f"📄 **{title}**: {label.lower()} ready (PDF attached). ⚠️ Email not sent: {exc}"})
        return Done({"pdf": str(pdf), "emailed": to, "mail_error": None,
                     "answer": f"📧 **{title}**: {label.lower()} emailed to {to}.\n\nPDF: `{pdf}`"})

    return {"proposal": proposal, "proposal_check": proposal_check, "proposal_render": proposal_render,
            "proposal_mail": proposal_mail}
