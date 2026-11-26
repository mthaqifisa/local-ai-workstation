"""Project lifecycle jobs (see law.projects for the states and gates).

- `project_new`: create the folder and git repo (or clone `repo_url`), then run the proposal pipeline and stop at
  the go/no-go gate.
- `project_step`: apply the owner's decision from Telegram and run the next phase: a revised proposal, the
  development pipeline, or a push.
- `project_gate`: the last stage of each phase: records the new state and returns the question with its buttons.

Development: an environment stage (install and select the tools the stack needs, recorded in .law/), then one
stage per milestone from .law/PLAN.md, with a fresh agent session per ticket and LAW's run of `.law/check.sh` after
each (ADR-0019), then up to three
independent review and fix rounds, then the verification gate.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from law import book_kit, bugs, specs
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
BUGFIX_ROUNDS = 3  # /bugfix: fix -> QA retest, at most this many times

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
- Java / Spring Boot (Maven): Spotless (google-java-format) and Checkstyle bound to `verify`, JaCoCo with a
  check of at least 80% line coverage, integration tests on Testcontainers; gate `mvn verify` (all of them).
  Credentials come from environment variables or an uncommitted .env (commit .env.example); databases and
  other internal services publish ports on 127.0.0.1 only, the app itself on 0.0.0.0.
- Go: gate `gofmt -l .` (empty), `go vet ./...` and `go test ./...`.
- Garmin Connect IQ (Monkey C): manifest.xml, monkey.jungle, source/, resources/. Gate: `monkeyc -d <device> -f
  monkey.jungle -o bin/<app>.prg -y "$CIQ_DEVELOPER_KEY" -w` for every target device, and the (:test) unit tests:
  build with `-t`, start the simulator (`open -a ConnectIQ`, wait ~10 s) and run `monkeydo bin/<app>.prg <device>
  -t`."""

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

CODE_TICKETS = """Split this agreed plan into tickets for the project board. Reply with Markdown only, nothing before
or after it: one level-3 heading per milestone ("### M1 Data layer"), and under each, small tickets, one per line:
"- [ ] {key}-{start} Title (acceptance: what proves it works) [est 40m]". Number the tickets in one sequence across
the milestones, starting at {key}-{start} (lower numbers belong to earlier versions).
[est …] is your honest estimate of the wall-clock time an AI agent on a local model needs for the ticket, reading,
writing, building and checking included: minutes (m), hours (h) or days (d), e.g. [est 25m], [est 1.5h]. Do not
round everything to the same value; LAW compares them with the real times and corrects the countdown.

The plan (version {version}):
{plan}"""

DEV_SPEC = """Build {name} version {version} as specified in {spec_file}{scope}. The tools are ready
(.law/ENVIRONMENT.md); do not change versions without updating it. Do not re-check the environment, the installed
tools or the library versions the environment stage recorded: start on the work, and look into a tool only when a
build error points at it.

Work like a team on a Jira board, in .law/PLAN.md:
- If there is no section "## Build version {version}", create it first, with exactly that heading. Split the work
  into 3 to 8 milestones (epics) in build order, each a level-3 heading such as "### M1 Backend API". Under each,
  list small tickets as checkboxes with an ID and acceptance criteria, e.g.
  "- [ ] {key}-3 Join rejects a second username from the same device (acceptance: unit + integration test) [est 40m]"
  ([est …]: your honest wall-clock estimate in m, h or d; the board's countdown uses it).
  A ticket another specialist should do carries its role, e.g. "[role writer]" for a user guide in a software
  project, "[role coder]" for a service in a document project, "[role designer]" for visuals, "[role analyst]" for a
  data study; LAW runs it with that role's instructions, tools and model. Untagged tickets use this stage's role.
  A ticket is one focused change and one commit; a milestone ends with a passing build and tests.
- A design document or reference (docs/design/) is a requirement: every screen and component it names gets a ticket.
  A UI ticket is done when you have run the product, opened the screen at a phone viewport (390 x 844) with real data,
  looked at the screenshot and it matches the design; tests passing alone do not show that.
- Keep tickets small: one endpoint, one part of a screen or one script, a few files, at most about two hours of
  work. A ticket that turns out bigger is split in .law/PLAN.md into "{key}-<n>.1", "{key}-<n>.2", ... (they
  replace its line, each with its own [est …]); then do the first part.
- Keep the code simple and the files small (about 400 lines at most per source or test file). A ticket that
  must change a file already over that size is preceded by a ticket "{key}-<next n> Split <file>" (behaviour
  unchanged, tests green), added to .law/PLAN.md right before it.
- Each session does ONE ticket: the one LAW names at the end of this prompt (the first unticked one). Write its
  code and tests, run the build and tests until they pass, and stop with a short reply. LAW then runs the gate,
  commits the work as the ticket, ticks it and records your reply in .law/PROGRESS.md. A fresh session takes the
  next ticket and knows only what is on disk and your reply: put in it what the next session needs (decisions,
  gotchas, commands).
- `.law/check.sh` is the project's whole gate: the build, formatting and lint, and every test (the platform's gate
  below), exiting non-zero on any failure. Create it with the first ticket (now, if it is missing), keep it
  complete as the project grows, and never weaken it to make it pass. LAW runs it after every ticket; a failure
  goes to the next session to fix first. It runs the gates the project has. If the platform gate asks for
  tooling the project lacks (e.g. Spotless, Checkstyle, JaCoCo), do not add it inside the current ticket and do
  not weigh it up: add a ticket "{key}-<next n> Add the missing gate tooling" right after the current one in
  .law/PLAN.md, and carry on.
- The last milestone includes a README that explains how to install, run and test the product, including from
  another device if it is a network service.
- Nobody answers questions during a build session: never stop to ask the owner or wait for a reply. Decide on the
  evidence you have. A check only the owner can do (a message arriving on their phone, a camera, a device) is
  written in your reply as "owner to confirm: ..." and the ticket is done when its automated part passes; the
  handover lists it for the owner. Clean up test data you created before you stop.
- Anything that reaches the owner for real (a Telegram message, an email, a notification) is sent at most once per
  milestone, only to prove the live path; everything else is tested with fakes or a dry run. Keep the response of
  that one call as the evidence instead of sending again. The build is complete only
  when every milestone in .law/PLAN.md is ticked.

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
   apps: run in the Connect IQ simulator (`open -a ConnectIQ`, `monkeydo bin/<app>.prg <device>`) and capture its
   window with `peekaboo image --app "Connect IQ Device Simulator" --path <file>`. Web apps (1280x800): a headless
   browser: Chrome headless (`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new
   --screenshot=<file> --window-size=1280,800 <url>`) for single pages, or a short puppeteer-core script (install it
   in a temp folder with npm; the browser is the chrome-headless-shell under ~/LAW/runtime/chrome) for steps that
   need typing or clicking. Stop everything you started when done.
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
   "## Handover v{version} sections".
Include a section "Check on your phone" (or the device the product targets): the steps only a person holding the
real device can do, taken from the requirements (e.g. trust the certificate, the camera scan, a notification
arriving), each with what should happen. QA tested everything else in a browser; say so."""

DOC_PLAN = """Plan this document (a book, report or research paper) for the owner to approve in a chat message.
Plain Markdown, at most about 450 words, no preamble, with exactly these parts:
**What I'll write**: the document, its readers and its formats in two or three sentences.
**Outline**: every part and chapter in order, each with its main sources (standards, official documents, papers)
and each diagram it will have, by name (a cover, when the document has one, is a part of its own). Each diagram
shows something no other diagram does (the ch.1 trial drew the same licensing path twice), and each fact has one
home section that the others refer back to.
**Milestones**: 3 to 8 parts in writing order, each ending with a PDF and HTML build that passes .law/check.sh.
Each chapter starts with a research ticket (primary sources read, every fact recorded with its source in
.law/RESEARCH.md); its writing tickets follow. LAW's book kit is already installed: plan no tooling or template work.
**Readers**: 3 to 5 kinds of reader the document must serve, from the request (who they are and what each needs
from it), one per line starting with "- "; if the request names none, choose them from its subject and purpose.
**Style**: voice, depth, diagram and citation conventions (kept in .law/STYLE.md).
**Assumptions**: the defaults you chose where the request is silent.
**Out of scope**: what the document will not cover.
Readers, structure, layers and style come from the request and the files it names; where they say something, follow
it word for word and do not invent your own. Name sources by their title only. State no facts, figures, dates or
section numbers in the plan: the writer finds and cites them from the sources while writing.

The REQUEST:
{request}

Project folder: {folder}
Files there now:
{listing}

Plan and progress so far:
{memory}"""

# Text files the owner names in a request (a brief, a spec): the planner is one model call and cannot open them.
NAMED_FILE = re.compile(r"(?<![\w/])(~/[^\s;,()\"'`]+|/(?:Users|home)/[^\s;,()\"'`]+)")
NAMED_SUFFIXES = {".md", ".txt", ".rst", ".adoc", ".typ", ".yaml", ".yml", ".json", ".csv"}
NAMED_MAX_CHARS = 60_000


KIT_NOTE = """

LAW's book kit is installed in this project (2026-11-24: a plan that did not know it planned Typst files, figs/,
bracketed citations and four "integrate and rebuild" tickets). Plan and ticket only the content: the build is one
command and runs after every ticket, so no ticket integrates, assembles or rebuilds. Follow its conventions:

{style}"""


def kit_conventions(folder: Path) -> str:
    """The installed book kit's style guide, for the planner of a document project ("" when it has none)."""
    style = folder / ".law" / "STYLE.md"
    return KIT_NOTE.format(style=style.read_text().strip()) if style.is_file() else ""


def named_files(text: str, home: Path | None = None) -> str:
    """The text of each readable text file the request names, as a block to append to it ("" when none)."""
    home = home or Path.home()
    out: list[str] = []
    seen: set[Path] = set()
    for raw in NAMED_FILE.findall(text):
        name = raw.rstrip(".:!?]")
        path = (home / name[2:]) if name.startswith("~/") else Path(name)
        try:
            path = path.resolve()
            if path in seen or path.suffix.lower() not in NAMED_SUFFIXES or not path.is_file():
                continue
            body = path.read_text(errors="replace")
        except OSError:
            continue
        seen.add(path)
        cut = "\n[... cut: the file is longer; read it in full while working]" if len(body) > NAMED_MAX_CHARS else ""
        out.append(f"--- {name} ---\n{body[:NAMED_MAX_CHARS]}{cut}")
    return ("\n\nFILES THE REQUEST NAMES (their full text; they are part of the request):\n\n"
            + "\n\n".join(out)) if out else ""


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

DOC_RULES = """

This is a document project (a book, report or guide). Its tickets are research, chapters and sub-chapters, not
features:
- LAW installed its tested book kit: book.md (metadata), chapters/, diagrams/, tools/build.sh, tools/verify.py and
  .law/STYLE.md, which says how to use them. Write the book; do not build, probe or replace the tooling, and do not
  experiment with pandoc or Typst in scratch folders. Change tools/ only when a check is wrong, and say why in
  .law/PROGRESS.md.
- Research before writing: a research ticket reads the primary sources for its chapter, saves each one it uses in
  sources/ (the PDF or page, plus its text from pdftotext; the editor checks quotes against them, and sources/ is
  not committed), and records every fact in .law/RESEARCH.md under the chapter's heading: the fact, the source
  (who, title, version or date, clause or page), the link, the file in sources/ and the exact quote or figure.
  A writing ticket uses only facts recorded there (add any it still needs, the same way, before writing them). A
  fact that cannot be verified is not written. Before writing that something is unknown, search for it (`law
  search`): a figure or fact a reader would expect (a regulator's limit, the names in an announcement, a launch
  date) is found and recorded, not left as a gap. The book speaks to the reader: never write about "the sources
  we hold"; say what is not published, and by whom, only when the search confirms it.
- State each fact once, in the section it belongs to. Before writing a section, list the headings of the chapter
  so far (`rg -n "^#" chapters/<file>`) and search it for the facts you will use: refer back to a fact already
  stated ("see <section>") instead of restating it.
- The readers in .law/STYLE.md ("Readers and voice") judge every chapter: write so each of them is served. Open
  each `##` section with an "In short" paragraph, keep sentences short (verify.py checks sentence length, dashes,
  long quotations and sequence-diagram labels in the text your ticket writes, against the limits of the document's
  kind), and run `python3 tools/verify.py --all` before you finish.
- Write for a reader meeting each idea for the first time. Before you use a term, product, acronym or idea (a
  database, double-entry, an idempotency key), search the whole book in reading order for it
  (`rg -n -i -w --sort path "<term>" book.md chapters/ | head -5`). If this is its first appearance, explain it here
  in plain words: what it is and why it comes up now, connected to what the reader has just read. If it is
  explained elsewhere, point there ("see 7.1"); if it is explained only later, move the explanation to the first
  use. Never let a reader meet a tool or concept with no idea what it is or why it is here (owner, 2026-11-26).
- One ticket = one chapter or sub-chapter in its own file (chapters/07-ledger.md) with its citations and diagrams
  (diagrams/<name>.mmd or .dot). Write it completely in one session.
- Read only what the ticket needs: its entries in .law/RESEARCH.md (search for them; they are verified quotes, so
  write from them) and .law/STYLE.md. Open sources/ only to look up one specific fact the notes lack; do not page
  through a source again. Do not re-read other chapters; .law/STYLE.md holds the voice, glossary terms and
  decisions every chapter must follow (add to it when you decide something others must match).
- `.law/check.sh` is the gate: it builds the PDF and HTML and fails, with a list, on a build error, a missing image,
  a citation without a link, a diagram whose text would be unreadable or that is taller than a page (split it), or a
  code line too long for the page. The UI and test-suite rules above do not apply. After the gate, look at the
  rendered pages of your chapter (`pdftoppm -png -r 70 -f <first> -l <last> book.pdf ~/LAW/cache/tmp/<name>`) and
  fix what reads or looks wrong.
- Cosmetic details get two tries; then keep what works and move on."""

DOC_ENV = """This stage checks the Mac for a document project; write no content.
LAW installed its tested book kit (tools/, .law/check.sh, .law/STYLE.md). Run `bash .law/check.sh` once. If it
passes, record the versions of pandoc, typst, mmdc and dot in .law/ENVIRONMENT.md, write .law/env.sh with
`export PATH="/opt/homebrew/bin:$PATH"`, commit .law/ and stop: do not test the tools any further. If it fails,
fix only the tool it names (install it with Homebrew), run it again, then record and commit as above."""

DOC_RUBRIC = """You are reviewing a chapter of a book for a demanding professional reader. Score it 1-5:
5 = accurate, complete for its scope, every claim cited, and convincing to every reader .law/STYLE.md names, each
in the terms that reader cares about, with short clear sentences (`python3 tools/verify.py --all` reports nothing for
the chapter), diagrams correct and readable within the page; 4 = solid, cosmetic issues only; 3 = one material gap,
error, uncited claim or unreadable diagram; 2 = several; 1 = wrong or missing.
Check facts and numbers against the cited sources, consistency with .law/STYLE.md and the outline, that each term,
tool or idea is explained where the reader first meets it in the whole book (search the book in reading order:
`rg -n -i -w --sort path "<term>" book.md chapters/ | head -5`; an unexplained first use, or one explained only later,
is a material gap), that the rendered pages look right (run .law/check.sh and look at the page images), and
that nothing is padding.
Reply with JSON only: {"score": <1-5>, "issues": ["<file:section problem and fix>"], "summary": "<one sentence>"}"""


def spec_file(a: dict[str, Any]) -> str:
    """What development builds from: the proposal (full projects) or the agreed plan in .law/ (code-only)."""
    return str(a.get("spec_file") or f"{a['out']}/proposal.md")


@specs.register("project.env")
def env_spec(a: dict[str, Any]) -> str:
    return DOC_ENV if a.get("kind") == "document" else ENV_SPEC.format(spec_file=spec_file(a))


def law_services() -> str:
    """How software the agents build calls LAW itself, from the registry (seen 2026-10-25: an app defaulted to the
    agents' 27B model, ~15-60 s a call and competing with the agents, and to 127.0.0.1 inside its container)."""
    from law import registry
    from law.paths import LawPaths

    reg = registry.load(LawPaths.from_env().registry_file)
    fast = next((m.id for m in reg.llm_models if "fast" in m.roles), "")
    return (
        "\n\nSoftware that calls LAW itself (its OpenAI-compatible LLM API on port "
        f"{reg.ports.llm_public}, SearXNG on port {reg.ports.searxng}):\n"
        f"- In-app calls use LAW's fast model `{fast}` (seconds per answer); the large model the agents use is slow "
        "and shared with them. Keep the model id a setting.\n"
        "- From a Docker container the Mac is `host.docker.internal` (127.0.0.1 is the container itself); from the "
        "Mac itself use 127.0.0.1. Make the base URLs settings, with the container value set in docker-compose.yml.\n"
        "- LAW can be busy or unloaded: set a timeout, treat LAW as optional, and degrade gracefully. HTTP 507 means "
        "another model holds LAW's memory (e.g. an agent is working): tell the user LAW is busy and to try again "
        "later; it is not an answer. The first call after idle loads the model (~10 s).\n"
        "- To message the owner (a reminder, an alert): `law send --message '...'` on the Mac; from a container "
        f"POST http://host.docker.internal:{reg.ports.law_api}/send with JSON {{\"text\": \"...\"}} (Telegram, "
        "the owner's chats only). A container cannot run the Mac's `law`. Never store a bot token in the project."
    )


@specs.register("project.dev")
def dev_spec(a: dict[str, Any]) -> str:
    v = int(a["version"])
    scope = "" if v == 1 or a.get("spec_file") else f', including its section "What changed in version {v}"'
    spec = DEV_SPEC.format(name=a["name"], version=v, spec_file=spec_file(a), scope=scope, key=a["key"])
    return spec + (DOC_RULES if a.get("kind") == "document" else law_services())


@specs.register("project.fix")
def fix_spec(a: dict[str, Any]) -> str:
    return FIX_SPEC.format(spec_file=spec_file(a), version=a["version"]) + (
        DOC_RULES if a.get("kind") == "document" else law_services())


@specs.register("project.handover")
def handover_spec(a: dict[str, Any]) -> str:
    return HANDOVER_SPEC.format(name=a["name"], version=a["version"], hout=HANDOVER_OUT)


@specs.register("project.review")
def review_rubric(a: dict[str, Any]) -> str:
    from law.handlers.tasks import CODE_RUBRIC

    rubric = DOC_RUBRIC if a.get("kind") == "document" else CODE_RUBRIC
    return rubric + REVIEW_CONTEXT.format(spec_file=spec_file(a), version=a["version"])


LEFT = re.compile(r"^\s*- \[ \]\s*(.+)$", re.MULTILINE)  # unticked milestones in a build section


def make_handlers(rt: Runtime) -> dict[str, Any]:
    store = Projects(rt.paths)

    def _gate(project: Project, gate: str) -> dict[str, Any]:
        return {"kind": "project_gate", "payload": {"project": project.id, "gate": gate}}

    def _proposal(project: Project) -> list[dict[str, Any]]:
        # documents use the writer's and researcher's own thinking level; the project's applies to its code
        folder = Path(project.dir)
        # a later version with no document yet (an adopted project) is a first proposal: research it, nothing to revise
        version = project.version if (folder / OUT / "proposal.md").exists() else 1
        stages = proposal_stages(rt, project.request(), folder, OUT, version=version, feedback=project.feedback,
                                 depth=project.proposal, extra={"project": project.id})
        return [*stages, {"kind": "project_tickets", "payload": {"project": project.id}}, _gate(project, "proposal")]

    def _plan(project: Project) -> list[dict[str, Any]]:
        """Code-only projects: a short plan in the chat instead of research, a proposal and email."""
        return [{"kind": "code_plan", "payload": {"project": project.id}}, _gate(project, "plan")]

    def _first_phase(project: Project) -> list[dict[str, Any]]:
        return _plan(project) if project.mode == "code" else _proposal(project)

    def _base(project: Project) -> dict[str, Any]:
        v = project.version
        args = {"out": OUT, "version": v, "name": project.name,
                "key": re.sub(r"[^A-Z]", "", project.name.title())[:4] or "PRJ"}  # ticket prefix: "Lan Chat" -> LC
        if project.mode == "code":
            args["spec_file"] = ".law/PROMPT.md (the agreed plan)"
        role = "writer" if project.kind == "document" else "coder"  # a book needs the writer's PDF and diagram rules
        args["kind"] = project.kind
        return {"prompt": project.request(), "dir": project.dir, "role": role, "version": v,
                "effort": project.effort, "project": project.id, "spec_args": args}

    def _develop(project: Project) -> list[dict[str, Any]]:
        v = project.version
        base = _base(project)
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

    async def _install_kit(ctx: Context, project: Project) -> None:
        """A document project starts with LAW's tested book kit, committed, so no session builds its own tooling."""
        folder = Path(project.dir)
        kit = rt.paths.repo / "templates" / "rnd"
        added = book_kit.install(kit, folder, project.name)
        if not added:
            upgraded = book_kit.upgrade(kit, folder)
            if upgraded:
                await ctx.progress(f"📚 LAW book kit updated: {', '.join(upgraded)}")
                await run_cmd(ctx, ["git", "add", "--", *upgraded], cwd=folder, timeout_s=60)
                await run_cmd(ctx, ["git", "commit", "-q", "-m", "LAW book kit update: " + ", ".join(upgraded)],
                              cwd=folder, timeout_s=60)
            return
        await ctx.progress(f"📚 LAW book kit installed ({len(added)} files: build, checks, style guide)")
        await run_cmd(ctx, ["git", "add", "--", *added], cwd=folder, timeout_s=60)
        await run_cmd(ctx, ["git", "commit", "-q", "-m", "LAW book kit: build, checks and style guide"], cwd=folder,
                      timeout_s=60)

    async def project_new(ctx: Context) -> Outcome:
        p = ctx.job.payload
        mode = p.get("mode", "project")
        try:
            project = store.create(p["fields"], p.get("chat"), mode)
        except TemplateError as exc:
            raise Fatal(str(exc)) from exc
        await _prepare_repo(ctx, project)
        if project.kind == "document":
            await _install_kit(ctx, project)
        store.save(project)
        await ctx.progress(f"📁 {project.dir}: " + ("planning" if mode == "code" else
                                                     "researching and writing proposal v1"))
        return Stages(_first_phase(project))

    async def project_tickets(ctx: Context) -> Outcome:
        """The proposal's tickets, on the board before the owner's go, so the go covers both. The build sessions work
        from them; without any, the first session writes them (DEV_SPEC)."""
        project = store.get(str(ctx.job.payload["project"]))
        document = Path(project.dir) / OUT / "proposal.md"
        if not document.exists():
            return Done({})
        await ctx.progress("🗂 writing the tickets for the board")
        board = ProjectMemory(Path(project.dir))
        board.drop_unstarted_builds()  # a revised proposal replaces the tickets of one nobody started
        key = _base(project)["spec_args"]["key"]
        tickets = (await answer(rt, "translator", [{"role": "user", "content": CODE_TICKETS.format(
            plan=document.read_text(), version=project.version, key=key, start=board.next_ticket(key))}],
            ctx)).strip()
        if LEFT.search(tickets):
            board.add_build_section(f"## Build version {project.version}\n\n{tickets}\n")
        return Done({})

    async def code_plan(ctx: Context) -> Outcome:
        """The plan the owner approves in the chat; it is the brief every build session works from."""
        project = store.get(str(ctx.job.payload["project"]))
        folder = Path(project.dir)
        memory = ProjectMemory(folder)
        template = DOC_PLAN if project.kind == "document" else CODE_PLAN
        request = project.request()
        prompt = template.format(request=request + named_files(request), folder=folder,
                                  listing=project_listing(folder), memory=memory.context() or "(none)")
        kit = kit_conventions(folder) if project.kind == "document" else ""
        prompt += kit
        await ctx.progress(f"🗺 planning v{project.version}")
        plan = (await answer(rt, "translator", [{"role": "user", "content": prompt}], ctx)).strip()
        if not plan:
            raise Fatal("the planner returned no plan")
        memory.prepare(ctx.job.parent or ctx.job.id, project.request(), plan)  # .law/PROMPT.md = the agreed plan
        await ctx.progress("🗂 writing the tickets for the board")
        memory.drop_unstarted_builds()  # a revised plan replaces the tickets of one nobody started
        key = _base(project)["spec_args"]["key"]
        tickets = (await answer(rt, "translator", [{"role": "user", "content": CODE_TICKETS.format(
            plan=plan, version=project.version, key=key, start=memory.next_ticket(key)) + kit}], ctx)).strip()
        if LEFT.search(tickets):  # the board shows them before the go; the build sessions work from them
            memory.add_build_section(f"## Build version {project.version}\n\n{tickets}\n")
        return Done({"plan": plan})

    async def project_step(ctx: Context) -> Outcome:
        p = ctx.job.payload
        if p.get("action") == "email":  # resend the project's latest document by email (not a decision)
            return await _email_latest(ctx, str(p["project"]))
        try:
            project = store.get(str(p["project"]))
            phase = decide(project, str(p["action"]), str(p.get("note", "")))
            if p.get("proposal") and phase == "proposal":
                project.mode = "project"  # the owner asked for a proposal document, not only a plan
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
            await _prepare_repo(ctx, project)  # an adopted project may have no git repository yet
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

    async def project_bugfix(ctx: Context) -> Outcome:
        """`/bugfix`: the coder fixes the unresolved bugs in .law/BUGS.md, then QA retests the product (and finds
        what else broke), up to BUGFIX_ROUNDS times; the owner gets the result in the chat."""
        project = store.get(str(ctx.job.payload["project"]))
        found = bugs.unresolved(Path(project.dir))
        if not found:
            return Done({"answer": f"✅ **{project.name}**: no open bugs in `.law/BUGS.md`."})
        names = ", ".join(b.id for b in found)
        review = {**_base(project), "spec_ref": "project.fix", "rubric_ref": "project.review", "bugs": names}
        stages: list[dict[str, Any]] = [{"kind": "task_fix", "payload": {**review, "issues": bugs.as_issues(found)}}]
        for _ in range(BUGFIX_ROUNDS - 1):
            stages += [{"kind": "task_review", "payload": review}, {"kind": "task_fix", "payload": review}]
        stages += [{"kind": "task_review", "payload": review},
                   {"kind": "project_bug_report", "payload": {"project": project.id, "bugs": names}}]
        await ctx.progress(f"🐞 fixing {names}; QA retests after each fix")
        return Stages(stages)

    async def project_qa(ctx: Context) -> Outcome:
        """`/qa` and `law qa`: QA tests the product (no code review, no fixes) and reports. Only ever on request."""
        p = ctx.job.payload
        project = store.get(str(p["project"]))
        run = f"QA run {time.strftime('%Y-%m-%d %H:%M')}"
        review = {**_base(project), "spec_ref": "project.fix", "rubric_ref": "project.review", "qa_only": True,
                  "qa_run": run}
        await ctx.progress(f"🧪 {run}: testing {project.name}")
        return Stages([{"kind": "task_review", "payload": review},
                       {"kind": "project_bug_report", "payload": {"project": project.id, "qa_run": run}}])

    async def project_bug_report(ctx: Context) -> Outcome:
        project = store.get(str(ctx.job.payload["project"]))
        if ctx.job.payload.get("qa_run"):
            found = bugs.unresolved(Path(project.dir))
            hold = [b for b in found if b.severity in bugs.BLOCKING]
            listing = "\n".join(f"- {b.id} {b.severity}, {b.status}: {b.title}" for b in found)
            text = (f"🧪 **{project.name}**: QA found no open bugs." if not found else
                    f"🧪 **{project.name}**: {len(hold)} blocking and {len(found) - len(hold)} minor open bug(s).\n"
                    f"{listing}\n\nSend `/bugfix {project.id}` to fix them all.")
            return Done({"answer": text})
        named = {x.strip() for x in str(ctx.job.payload.get("bugs", "")).split(",") if x.strip()}
        mine = [b for b in bugs.load(Path(project.dir)) if b.id in named or b.reporter.lower() == "qa"]
        lines = [f"- {b.id} {b.status}: {b.title}" for b in mine if b.id in named or b.status in bugs.UNRESOLVED]
        open_count = sum(1 for b in mine if b.status in bugs.UNRESOLVED)
        head = "✅ fixed and verified by QA" if open_count == 0 else f"⚠️ {open_count} still open"
        return Done({"answer": f"🐞 **{project.name}** bug fix: {head}.\n" + "\n".join(lines)})

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
            held = ProjectMemory(Path(project.dir)).pending_notes()
            waiting = f"\n\n📝 Not built yet, your notes during the handover:\n{held}" if held else ""
            status = (f"⚠️ {len(left)} milestone(s) not finished: " + "; ".join(left[:5]) + "\n\n") if left else ""
            handover = ("the handover document (how to run and use it, with screenshots) is "
                        + ("in your email and here" if carry.get("emailed") else "attached here (email not sent)"))
            text = (f"✅ **{project.name} v{project.version}** is built. Independent review: {score}"
                    f"{' (' + review.get('summary', '') + ')' if review.get('summary') else ''}.\n\n{status}"
                    f"To try it, {handover if carry.get('pdf') else 'see README.md in `' + project.dir + '`'}; "
                    f"progress is in `.law/PROGRESS.md`.{waiting}\n\nIs it good, or what should change?")
            actions = ["allgood", "changes", *unsent]
        store.save(project)
        return Done({"answer": text, "gate": {"project": project.id, "actions": actions}})

    return {"project_new": project_new, "project_step": project_step, "project_gate": project_gate,
            "code_plan": code_plan, "project_tickets": project_tickets, "project_bugfix": project_bugfix,
            "project_bug_report": project_bug_report,
            "project_qa": project_qa}
