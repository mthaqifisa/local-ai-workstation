"""Project lifecycle jobs (see law.projects for the states and gates).

- `project_new`: create the folder and git repo (or clone `repo_url`), then run the proposal pipeline and stop at
  the go/no-go gate.
- `project_step`: apply the owner's decision from Telegram and run the next phase: a revised proposal, the
  development pipeline, or a push.
- `project_gate`: the last stage of each phase: records the new state and returns the question with its buttons.

Development: an environment stage (install and select the tools the stack needs, recorded in .law/), then one
agent session per milestone from .law/PLAN.md (fresh context each time, so it never fills up), then up to three
independent review and fix rounds, then the verification gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from law import mailer, specs
from law.handlers.base import Runtime, run_cmd
from law.handlers.proposal import proposal_stages
from law.handlers.tasks import build_section
from law.projects import Project, Projects, TemplateError, decide
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

OUT = "docs/proposal"
HANDOVER_OUT = "docs/handover"
MILESTONE_STAGES = 12  # unused stages finish at once when every milestone is ticked
REVIEW_ROUNDS = 3

ENV_SPEC = """This stage prepares the Mac for the project; write no application code.
Read {out}/proposal.md (the stack and technical design). For every tool the project needs (language runtimes with
their exact major versions, build tools, package managers, databases, browsers for tests), check what is installed
(e.g. `java -version`, `/usr/libexec/java_home -V`, `node -v`, `brew list --versions`). You have full access to
this Mac: install what is missing or the wrong version with Homebrew (e.g. `brew install openjdk@21`) and verify it
runs. Prefer the current LTS versions the proposal's libraries support.
Write `.law/env.sh` with the exports this project needs (e.g. `export JAVA_HOME=...`, `export PATH=...:$PATH`);
LAW loads it into every later agent session for this project. Record each tool in `.law/ENVIRONMENT.md`: required
version, installed version, how it is selected. Commit `.law/`."""

DEV_SPEC = """Build {name} version {version} as specified in {out}/proposal.md{scope}. The tools are ready
(.law/ENVIRONMENT.md); do not change versions without updating it.

Work like a team on a Jira board, in .law/PLAN.md:
- If there is no section "## Build version {version}", create it first, with exactly that heading. Split the work
  into 3 to 8 milestones (epics) in build order, each a level-3 heading such as "### M1 Backend API". Under each,
  list small tickets as checkboxes with an ID and acceptance criteria, e.g.
  "- [ ] {key}-3 Join rejects a second username from the same device (acceptance: unit + integration test)".
  A ticket is one focused change and one commit; a milestone ends with a passing build and tests.
- Then work on ONLY the first milestone that still has unticked tickets. Take its tickets in order: write the code
  and tests, run the build and tests until they pass, commit as "{key}-<n>: <summary>", tick the ticket, and add a
  line to .law/PROGRESS.md. When the milestone's tickets are all ticked, stop; the next session takes the next
  milestone. If you are interrupted, the next session continues from the first unticked ticket.
- The last milestone includes a README that explains how to install, run and test the product, including from
  another device if it is a network service."""

FIX_SPEC = """Fix in the codebase, run the full build and tests until they pass, commit, and record the fixes in
.law/PROGRESS.md. The specification is {out}/proposal.md (version {version})."""

HANDOVER_SPEC = """The build of {name} version {version} is finished, tested and reviewed. Write the SYSTEM HANDOVER
DOCUMENT in {hout}/handover.md (pandoc Markdown) so someone new can run, use and maintain it. Change no
application code in this stage.

1. Take real screenshots first. Start the app the way its README says (in the background), wait until it
   answers, and capture the key screens and steps of the main journeys (e.g. join, chat, error states) as PNG
   files in {hout}/assets/ (1280x800). Use a headless browser: Chrome headless
   (`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --screenshot=<file>
   --window-size=1280,800 <url>`) for single pages, or a short puppeteer-core script (install it in a temp folder
   with npm; the browser is the chrome-headless-shell under ~/LAW/runtime/chrome) for steps that need typing or
   clicking. Stop everything you started when done.
2. Then write the document, starting with this YAML header:
---
title: <product name>
subtitle: System handover (version {version})
kind: HANDOVER
prepared-for: <the client, from the request; otherwise "Owner">
author: LAW
date: <today>
---
   with these level-1 sections:
   Overview (what it is, who it is for, what is in this version) · Architecture and modules (a Mermaid container
   diagram in ```{{.mermaid caption="..."}}```, then every module and what it does) · Installation and running
   (prerequisites with versions, configuration, start/stop, opening it from another device) · User guide (a
   step-by-step tutorial of every feature, each step with its screenshot as ![<caption>](assets/<file>.png))
   · Operations (logs, data, backup, troubleshooting table) · Testing (how to run the tests, what they cover,
   the latest results) · Known limitations and next steps.
3. Every command and path in the document must work as written: run them to check. Write the file section by
   section (at most about 1,500 words per write) and tick each section in .law/PLAN.md under
   "## Handover v{version} sections"."""

REVIEW_CONTEXT = """

The specification is {out}/proposal.md (version {version}); the milestones are in .law/PLAN.md. Check the delivered
scope against them, and run the build and tests yourself."""

@specs.register("project.env")
def env_spec(a: dict[str, Any]) -> str:
    return ENV_SPEC.format(out=a["out"])


@specs.register("project.dev")
def dev_spec(a: dict[str, Any]) -> str:
    v = int(a["version"])
    scope = "" if v == 1 else f', including its section "What changed in version {v}"'
    return DEV_SPEC.format(name=a["name"], version=v, out=a["out"], scope=scope, key=a["key"])


@specs.register("project.fix")
def fix_spec(a: dict[str, Any]) -> str:
    return FIX_SPEC.format(out=a["out"], version=a["version"])


@specs.register("project.handover")
def handover_spec(a: dict[str, Any]) -> str:
    return HANDOVER_SPEC.format(name=a["name"], version=a["version"], hout=HANDOVER_OUT)


@specs.register("project.review")
def review_rubric(a: dict[str, Any]) -> str:
    from law.handlers.tasks import CODE_RUBRIC

    return CODE_RUBRIC + REVIEW_CONTEXT.format(out=a["out"], version=a["version"])


LEFT = re.compile(r"^\s*- \[ \]\s*(.+)$", re.MULTILINE)  # unticked milestones in a build section


def make_handlers(rt: Runtime) -> dict[str, Any]:
    store = Projects(rt.paths)

    def _gate(project: Project, gate: str) -> dict[str, Any]:
        return {"kind": "project_gate", "payload": {"project": project.id, "gate": gate}}

    def _proposal(project: Project) -> list[dict[str, Any]]:
        # documents use the writer's and researcher's own thinking level; the project's applies to its code
        stages = proposal_stages(rt, project.request(), Path(project.dir), OUT, version=project.version,
                                 feedback=project.feedback, depth=project.proposal, extra={"project": project.id})
        return [*stages, _gate(project, "proposal")]

    def _develop(project: Project) -> list[dict[str, Any]]:
        v = project.version
        args = {"out": OUT, "version": v, "name": project.name,
                "key": re.sub(r"[^A-Z]", "", project.name.title())[:4] or "PRJ"}  # ticket prefix: "Lan Chat" -> LC
        base: dict[str, Any] = {"prompt": project.request(), "dir": project.dir, "role": "coder", "version": v,
                                "effort": project.effort, "project": project.id, "spec_args": args}
        review = {**base, "spec_ref": "project.fix", "rubric_ref": "project.review"}
        stages: list[dict[str, Any]] = [
            {"kind": "task_work", "payload": {**base, "spec_ref": "project.env", "stall_min": 45}}]
        stages += [{"kind": "task_work", "payload": {**base, "milestone": True, "spec_ref": "project.dev"}}] \
            * MILESTONE_STAGES
        stages += [{"kind": "task_review", "payload": review}, {"kind": "task_fix", "payload": review}] * REVIEW_ROUNDS
        # the handover document (with real screenshots) is written, rendered and emailed before the owner verifies
        (Path(project.dir) / HANDOVER_OUT / "assets").mkdir(parents=True, exist_ok=True)
        doc = {"dir": project.dir, "out": HANDOVER_OUT, "doc": "handover", "version": v, "prompt": project.request()}
        stages += [{"kind": "task_work", "payload": {**base, "spec_ref": "project.handover", "stall_min": 45}},
                   {"kind": "proposal_render", "payload": doc}, {"kind": "proposal_mail", "payload": doc}]
        return [*stages, _gate(project, "verify")]

    async def _prepare_repo(ctx: Context, project: Project) -> None:
        folder = Path(project.dir)
        if not folder.exists() and project.repo_url:
            await ctx.progress(f"cloning {project.repo_url}")
            await run_cmd(ctx, ["git", "clone", project.repo_url, str(folder)], timeout_s=900)
            return
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / ".git").exists():
            await run_cmd(ctx, ["git", "init", "-q"], cwd=folder, timeout_s=60)
            if project.repo_url:
                await run_cmd(ctx, ["git", "remote", "add", "origin", project.repo_url], cwd=folder, timeout_s=60)

    async def project_new(ctx: Context) -> Outcome:
        p = ctx.job.payload
        mailer.recipient(rt.paths.env_file)  # the proposal is emailed: fail now if there is no address
        try:
            project = store.create(p["fields"], p.get("chat"))
        except TemplateError as exc:
            raise Fatal(str(exc)) from exc
        await _prepare_repo(ctx, project)
        store.save(project)
        await ctx.progress(f"📁 {project.dir}: researching and writing proposal v1")
        return Stages(_proposal(project))

    async def project_step(ctx: Context) -> Outcome:
        p = ctx.job.payload
        try:
            project = store.get(str(p["project"]))
            phase = decide(project, str(p["action"]), str(p.get("note", "")))
        except (KeyError, ValueError) as exc:
            # a refused decision (wrong moment, e.g. an old Go button; unknown project) is information, not a
            # failure: no Retry / Skip
            return Done({"answer": f"ℹ️ Not done: {str(exc).strip(chr(39) + chr(34))}", "refused": True})
        if p.get("chat"):
            project.chat = int(p["chat"])
        store.save(project)
        if phase == "proposal":
            await ctx.progress(f"✏️ writing proposal v{project.version}")
            return Stages(_proposal(project))
        if phase == "develop":
            await ctx.progress(f"🛠 starting development of v{project.version}")
            return Stages(_develop(project))
        if phase == "push":
            await run_cmd(ctx, ["git", "push", "-u", "origin", "HEAD"], cwd=Path(project.dir), timeout_s=600)
            return Done({"answer": f"⬆️ **{project.name}** pushed to {project.repo_url}."})
        if project.state == "parked":
            return Done({"answer": f"⏸ **{project.name}** is parked. Revive it with "
                                   f"`/resume {project.id} <what to change>`."})
        actions = ["push"] if project.repo_url else []
        return Done({"answer": f"🎉 **{project.name}** v{project.version} is done. Thank you!"
                               + (f" Push it to {project.repo_url} when you are ready." if actions else ""),
                     "gate": {"project": project.id, "actions": actions}})

    async def project_gate(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = p.get("_carry") or {}
        project = store.get(str(p["project"]))
        review = carry.get("review") or {}
        score = f"{review.get('score')}/5" if review else "n/a"
        if p["gate"] == "proposal":
            project.state = "awaiting_go"
            project.log("proposal emailed", f"v{project.version}, review {score}")
            where = ("is in your email (PDF attached here too)" if carry.get("emailed")
                     else f"is attached here (⚠️ email not sent: {carry.get('mail_error') or 'unknown reason'})")
            text = (f"📄 **Proposal v{project.version}: {project.name}** {where}. "
                    f"Independent review: {score}.\n\nShall I start development?")
            actions = ["go", "nogo"]
        else:
            project.state = "awaiting_verification"
            project.log("built", f"v{project.version}, review {score}")
            left = LEFT.findall(build_section(Path(project.dir), project.version) or "")
            status = (f"⚠️ {len(left)} milestone(s) not finished: " + "; ".join(left[:5]) + "\n\n") if left else ""
            handover = ("the handover document (how to run and use it, with screenshots) is "
                        + ("in your email and here" if carry.get("emailed") else "attached here (email not sent)"))
            text = (f"✅ **{project.name} v{project.version}** is built. Independent review: {score}"
                    f"{' (' + review.get('summary', '') + ')' if review.get('summary') else ''}.\n\n{status}"
                    f"To try it, {handover if carry.get('pdf') else 'see README.md in `' + project.dir + '`'}; "
                    "progress is in `.law/PROGRESS.md`.\n\nIs it good, or what should change?")
            actions = ["allgood", "changes"]
        store.save(project)
        return Done({"answer": text, "gate": {"project": project.id, "actions": actions}})

    return {"project_new": project_new, "project_step": project_step, "project_gate": project_gate}
