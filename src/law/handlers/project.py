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

from law import specs
from law.handlers.base import Runtime, run_cmd
from law.handlers.core import answer
from law.handlers.proposal import proposal_stages
from law.handlers.tasks import build_section
from law.project_memory import ProjectMemory, project_listing
from law.projects import Project, Projects, TemplateError, decide
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

OUT = "docs/proposal"
HANDOVER_OUT = "docs/handover"
MILESTONE_STAGES = 12  # unused stages finish at once when every milestone is ticked
REVIEW_ROUNDS = 3

# What each stack needs on this Mac and how it is built, tested and shown (ADR-0016). Shared by the stages below.
ENV_PLATFORMS = """Per platform (only what the project's stack needs):
- iOS / watchOS / macOS (Swift): Xcode and XcodeGen are installed. Pick an installed simulator
  (`xcrun simctl list devices available`) and record its destination, e.g. `platform=iOS Simulator,name=iPhone 17`,
  in .law/ENVIRONMENT.md and as `export XCODE_DESTINATION=...` in .law/env.sh.
- React Native: Node, CocoaPods and Watchman are installed; it runs on the iOS simulator (no Android SDK on this
  Mac: say so in ENVIRONMENT.md if the spec needs Android).
- Go: use the Go version go.mod declares (`go version`; Homebrew `go`).
- Garmin Connect IQ (Monkey C): the SDK is installed (`monkeyc --version`). Device definitions come from the
  Connect IQ SDK Manager with the owner's Garmin sign-in: check that the target devices exist in
  `~/Library/Application Support/Garmin/ConnectIQ/Devices`; if they do not, stop and report that the owner must
  open "Connect IQ SDK Manager", sign in and download them. The developer key is
  ~/LAW/state/keys/connectiq_developer_key.der; if it is missing create it (`mkdir -p ~/LAW/state/keys && cd
  ~/LAW/state/keys && openssl genrsa -out connectiq_developer_key.pem 4096 && openssl pkcs8 -topk8 -inform PEM
  -outform DER -in connectiq_developer_key.pem -out connectiq_developer_key.der -nocrypt && chmod 600 *`). Never
  copy it into the project; export `CIQ_DEVELOPER_KEY` in .law/env.sh."""

DEV_PLATFORMS = """Build and test per platform:
- iOS / watchOS (Swift, SwiftUI): describe the Xcode project in project.yml and generate it with `xcodegen generate`
  (commit project.yml; never hand-edit project.pbxproj). Gate: `xcodebuild test -scheme <App> -destination
  "$XCODE_DESTINATION" -quiet` (unit tests with Swift Testing or XCTest, UI flows with XCUITest).
- React Native: create the app with `npx create-expo-app@latest` (TypeScript) unless the spec asks for bare React
  Native; gate: `npx tsc --noEmit` and Jest with React Native Testing Library; run it with `npx expo run:ios`.
- Angular / React web: the framework CLI's build, lint and unit tests (e.g. `npx ng test --watch=false`), plus an
  end-to-end check against the running app.
- Go: gate `gofmt -l .` (empty), `go vet ./...` and `go test ./...`.
- Garmin Connect IQ (Monkey C): manifest.xml, monkey.jungle, source/, resources/. Gate: `monkeyc -d <device> -f
  monkey.jungle -o bin/<app>.prg -y "$CIQ_DEVELOPER_KEY" -w` for every target device, and the (:test) unit tests:
  build with `-t`, start the simulator (`connectiq &`) and run `monkeydo bin/<app>.prg <device> -t`."""

ENV_SPEC = """This stage prepares the Mac for the project; write no application code.
Read {spec_file} (the stack and technical design). For every tool the project needs (language runtimes with
their exact major versions, build tools, package managers, databases, browsers for tests), check what is installed
(e.g. `java -version`, `/usr/libexec/java_home -V`, `node -v`, `brew list --versions`). You have full access to
this Mac: install what is missing or the wrong version with Homebrew (e.g. `brew install openjdk@21`) and verify it
runs. Prefer the current LTS versions the proposal's libraries support.
Write `.law/env.sh` with the exports this project needs (e.g. `export JAVA_HOME=...`, `export PATH=...:$PATH`);
LAW loads it into every later agent session for this project. Record each tool in `.law/ENVIRONMENT.md`: required
version, installed version, how it is selected. Commit `.law/`.

""" + ENV_PLATFORMS

DEV_SPEC = """Build {name} version {version} as specified in {spec_file}{scope}. The tools are ready
(.law/ENVIRONMENT.md); do not change versions without updating it. Do not re-check the environment, the installed
tools or the library versions the environment stage recorded: start on the work, and look into a tool only when a
build error points at it.

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
  another device if it is a network service.

""" + DEV_PLATFORMS

FIX_SPEC = """Fix in the codebase, run the full build and tests until they pass, commit, and record the fixes in
.law/PROGRESS.md. The specification is {spec_file} (version {version})."""

HANDOVER_SPEC = """The build of {name} version {version} is finished, tested and reviewed. Write the SYSTEM HANDOVER
DOCUMENT in {hout}/handover.md (pandoc Markdown) so someone new can run, use and maintain it. Change no
application code in this stage.

1. Take real screenshots first. Start the app the way its README says (in the background), wait until it answers,
   and capture the key screens and steps of the main journeys (e.g. join, chat, error states) as PNG files in
   {hout}/assets/. iOS apps: build for the simulator, `xcrun simctl boot`, install and launch the app, then `xcrun
   simctl io booted screenshot <file>` for each screen (drive the steps with an XCUITest or deep links). Garmin
   apps: run in the Connect IQ simulator (`connectiq &`, `monkeydo bin/<app>.prg <device>`) and capture its window
   with `peekaboo image --app ConnectIQ --path <file>`. Web apps (1280x800): a headless browser: Chrome headless
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

CODE_PLAN = """Plan this coding work for the owner to approve in a chat message. Plain Markdown, at most about 350
words, no preamble, with exactly these parts:
**What I'll build**: the product in two or three sentences.
**Stack**: each technology with the exact version, chosen to work together (e.g. a framework version that supports
the JDK).
**Milestones**: 3 to 6, in build order, each ending with passing tests.
**How I'll test it**: unit, integration and end-to-end checks that prove it works.
**Assumptions**: the defaults you chose where the request is silent.
**Out of scope**: what you will not build.

The REQUEST:
{request}

Project folder: {folder}
Files there now:
{listing}

Plan and progress so far:
{memory}"""

REVIEW_CONTEXT = """

The specification is {spec_file} (version {version}); the milestones are in .law/PLAN.md. Check the delivered
scope against them, and run the build and tests yourself."""

def spec_file(a: dict[str, Any]) -> str:
    """What development builds from: the proposal (full projects) or the agreed plan in .law/ (code-only)."""
    return str(a.get("spec_file") or f"{a['out']}/proposal.md")


@specs.register("project.env")
def env_spec(a: dict[str, Any]) -> str:
    return ENV_SPEC.format(spec_file=spec_file(a))


@specs.register("project.dev")
def dev_spec(a: dict[str, Any]) -> str:
    v = int(a["version"])
    scope = "" if v == 1 or a.get("spec_file") else f', including its section "What changed in version {v}"'
    return DEV_SPEC.format(name=a["name"], version=v, spec_file=spec_file(a), scope=scope, key=a["key"])


@specs.register("project.fix")
def fix_spec(a: dict[str, Any]) -> str:
    return FIX_SPEC.format(spec_file=spec_file(a), version=a["version"])


@specs.register("project.handover")
def handover_spec(a: dict[str, Any]) -> str:
    return HANDOVER_SPEC.format(name=a["name"], version=a["version"], hout=HANDOVER_OUT)


@specs.register("project.review")
def review_rubric(a: dict[str, Any]) -> str:
    from law.handlers.tasks import CODE_RUBRIC

    return CODE_RUBRIC + REVIEW_CONTEXT.format(spec_file=spec_file(a), version=a["version"])


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

    def _plan(project: Project) -> list[dict[str, Any]]:
        """Code-only projects: a short plan in the chat instead of research, a proposal and email."""
        return [{"kind": "code_plan", "payload": {"project": project.id}}, _gate(project, "plan")]

    def _first_phase(project: Project) -> list[dict[str, Any]]:
        return _plan(project) if project.mode == "code" else _proposal(project)

    def _develop(project: Project) -> list[dict[str, Any]]:
        v = project.version
        args = {"out": OUT, "version": v, "name": project.name,
                "key": re.sub(r"[^A-Z]", "", project.name.title())[:4] or "PRJ"}  # ticket prefix: "Lan Chat" -> LC
        if project.mode == "code":
            args["spec_file"] = ".law/PROMPT.md (the agreed plan)"
        base: dict[str, Any] = {"prompt": project.request(), "dir": project.dir, "role": "coder", "version": v,
                                "effort": project.effort, "project": project.id, "spec_args": args}
        review = {**base, "spec_ref": "project.fix", "rubric_ref": "project.review"}
        stages: list[dict[str, Any]] = [
            {"kind": "task_work", "payload": {**base, "spec_ref": "project.env", "stall_min": 45}}]
        stages += [{"kind": "task_work", "payload": {**base, "milestone": True, "spec_ref": "project.dev"}}] \
            * MILESTONE_STAGES
        stages += [{"kind": "task_review", "payload": review}, {"kind": "task_fix", "payload": review}] * REVIEW_ROUNDS
        if project.mode == "code":  # code-only: no handover document; the verify question says how to run it
            return [*stages, _gate(project, "verify")]
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
        mode = p.get("mode", "project")
        try:
            project = store.create(p["fields"], p.get("chat"), mode)
        except TemplateError as exc:
            raise Fatal(str(exc)) from exc
        await _prepare_repo(ctx, project)
        store.save(project)
        await ctx.progress(f"📁 {project.dir}: " + ("planning" if mode == "code" else
                                                     "researching and writing proposal v1"))
        return Stages(_first_phase(project))

    async def code_plan(ctx: Context) -> Outcome:
        """The plan the owner approves in the chat; it is the brief every build session works from."""
        project = store.get(str(ctx.job.payload["project"]))
        folder = Path(project.dir)
        memory = ProjectMemory(folder)
        prompt = CODE_PLAN.format(request=project.request(), folder=folder, listing=project_listing(folder),
                                  memory=memory.context() or "(none)")
        await ctx.progress(f"🗺 planning v{project.version}")
        plan = (await answer(rt, "translator", [{"role": "user", "content": prompt}], ctx)).strip()
        if not plan:
            raise Fatal("the planner returned no plan")
        memory.prepare(ctx.job.parent or ctx.job.id, project.request(), plan)  # .law/PROMPT.md = the agreed plan
        return Done({"plan": plan})

    async def project_step(ctx: Context) -> Outcome:
        p = ctx.job.payload
        if p.get("action") == "email":  # resend the project's latest document by email (not a decision)
            return await _email_latest(ctx, str(p["project"]))
        try:
            project = store.get(str(p["project"]))
            phase = decide(project, str(p["action"]), str(p.get("note", "")))
        except (KeyError, ValueError) as exc:
            # a refused decision (wrong moment, e.g. an old Go button; unknown project) is information, not a
            # failure: no Retry / Skip
            return Done({"answer": f"🔸 Not done: {str(exc).strip(chr(39) + chr(34))}", "refused": True})
        if p.get("chat"):
            project.chat = int(p["chat"])
        store.save(project)
        if phase == "proposal":
            await ctx.progress(f"✏️ writing {'plan' if project.mode == 'code' else 'proposal'} v{project.version}")
            return Stages(_first_phase(project))
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

    async def _email_latest(ctx: Context, ref: str) -> Outcome:
        try:
            project = store.get(ref)
        except KeyError as exc:
            return Done({"answer": f"🔸 Not done: {str(exc).strip(chr(39) + chr(34))}", "refused": True})
        docs = Path(project.dir) / "docs"
        pdfs = sorted(docs.glob("*/*.pdf"), key=lambda f: f.stat().st_mtime) if docs.is_dir() else []
        if not pdfs:
            return Done({"answer": f"🔸 {project.name} has no document to email yet.", "refused": True})
        pdf = pdfs[-1]
        doc = "handover" if pdf.name.startswith("handover") else "proposal"
        payload = {"dir": project.dir, "out": str(pdf.parent.relative_to(project.dir)), "doc": doc,
                   "version": project.version, "prompt": project.request()}
        await ctx.progress(f"📧 emailing {pdf.name}")
        return Stages([{"kind": "proposal_mail", "payload": payload}], carry={"pdf": str(pdf)})

    async def project_gate(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = p.get("_carry") or {}
        project = store.get(str(p["project"]))
        review = carry.get("review") or {}
        score = f"{review.get('score')}/5" if review else "n/a"
        unsent = ["email"] if carry.get("pdf") and not carry.get("emailed") else []  # offer to resend
        if p["gate"] == "plan":
            project.state = "awaiting_go"
            project.log("plan ready", f"v{project.version}")
            text = (f"🗺 **Plan v{project.version}: {project.name}**\n\n{carry.get('plan', '')}\n\n"
                    "Shall I start? (No go with notes revises the plan.)")
            actions = ["go", "nogo"]
        elif p["gate"] == "proposal":
            project.state = "awaiting_go"
            project.log("proposal emailed", f"v{project.version}, review {score}")
            where = ("is in your email (PDF attached here too)" if carry.get("emailed")
                     else f"is attached here (⚠️ email not sent: {carry.get('mail_error') or 'unknown reason'})")
            notes = []
            if carry.get("diagrams_repaired"):
                notes.append(f"{carry['diagrams_repaired']} repaired automatically")
            if carry.get("diagrams_failed"):
                notes.append(f"{len(carry['diagrams_failed'])} still shown as source in the PDF")
            diagrams = f"\nDiagrams: {'; '.join(notes)}." if notes else ""
            text = (f"📄 **Proposal v{project.version}: {project.name}** {where}. "
                    f"Independent review: {score}.{diagrams}\n\nShall I start development?")
            actions = ["go", "nogo", *unsent]
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
            actions = ["allgood", "changes", *unsent]
        store.save(project)
        return Done({"answer": text, "gate": {"project": project.id, "actions": actions}})

    return {"project_new": project_new, "project_step": project_step, "project_gate": project_gate,
            "code_plan": code_plan}
