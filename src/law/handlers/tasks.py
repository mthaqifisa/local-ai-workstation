"""Search and multi-step task handlers (OpenCode runs the coding and research agents)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from law import specs
from law.handlers.base import Runtime, Stalled, Steered, run_cmd
from law.handlers.core import PASS_SCORE, answer, parse_verdict
from law.project_memory import FOLDER, ProjectMemory, project_listing
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

# Roles whose output is code or files get a translated brief, an independent review and one fix pass by default.
REVIEWED_ROLES = {"coder", "writer", "designer", "architect", "analyst"}

TRANSLATE = """Write the brief for a {role} agent.

The user's REQUEST:
{request}

Project folder: {folder}
Files there now:
{listing}

Plan and progress recorded so far:
{memory}{fixed}{choose}"""

# A pipeline (e.g. /proposal) that fixes the deliverables and structure: the brief must work within them.
FIXED = """

The agent will ALSO receive these fixed instructions from the pipeline, word for word. Your brief must not
contradict them or name other deliverables, files or structure; concentrate on the request's content (scope,
users, requirements, constraints, acceptance criteria) and leave format and file names to these instructions:
{spec}"""

# A coder request without a folder: the translator names the project folder; LAW validates and creates it, so
# every project gets its own folder (never ~/SourceCode itself, never an unrelated project).
CHOOSE_FOLDER = """

Existing projects in {root}: {projects}
Begin your reply with one line `Project folder: {root}/<name>`. Use an existing project's folder only when the
REQUEST refers to that project by name. Otherwise choose a new, short kebab-case name: the project name the user
gave, or one that describes the project. Then write the brief."""
PROJECT_LINE = re.compile(r"^\W*Project folder:\**\s*`?([^`\s]+)`?\s*$", re.MULTILINE | re.IGNORECASE)
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

NEW_PROJECT_MEMORY = ("Create the project in a new folder ~/SourceCode/<project-name>/, named after the project the "
                      "user named. Never write files directly in ~/SourceCode or into another project's folder.\n"
                      f"In the new project folder, create {FOLDER}/ first: {FOLDER}/PROMPT.md holding the brief "
                      f"below word for word, plus {FOLDER}/RESEARCH.md, {FOLDER}/PLAN.md (numbered steps with "
                      f"checkboxes and gates) and {FOLDER}/PROGRESS.md (done, gate output, next step), and keep "
                      f"them current after every step. Never write secrets into {FOLDER}/.")

CODE_RUBRIC = """You are reviewing work for production use at a bank. Score it 1-5:
5 = correct, complete, clean, well tested, docs accurate; 4 = solid, cosmetic issues only;
3 = works but has a real defect (wrong/misleading docs, dead code, missing edge case, weak tests);
2 = significant bugs or gaps; 1 = broken or missing.
Check: does it do what was asked; correctness and edge cases; tests meaningful; docstrings and README match the
code; no dead code; types and error handling. Run the project's checks if you can read how.
Reply with JSON only: {"score": <1-5>, "issues": ["<file:line problem and fix>"], "summary": "<one sentence>"}"""


async def searx(rt: Runtime, query: str, limit: int = 8) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{rt.reg.ports.searxng}/search"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"q": query, "format": "json"})
        r.raise_for_status()
        results = r.json().get("results", [])[:limit]
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in results]


TOOL_ICONS = {"write": "✏️ writing", "edit": "✏️ editing", "patch": "✏️ patching", "read": "📖 reading",
              "bash": "▶ running", "grep": "🔎 searching", "glob": "🔎 finding", "list": "📂 listing",
              "webfetch": "🌐 fetching", "todowrite": "🗒 planning"}


def project_folder(brief: str, root: Path) -> Path | None:
    """The folder the translator chose: a valid name directly under the projects root, or None."""
    match = PROJECT_LINE.search(brief)
    if not match:
        return None
    path = Path(match.group(1)).expanduser()
    if not path.is_absolute():
        path = root / path
    name = path.name.lower()
    if path.parent.resolve() != root.resolve() or not PROJECT_NAME.match(name):
        return None
    return root / name


def project_env(project: Path) -> dict[str, str]:
    """Variables `<project>/.law/env.sh` sets or changes (e.g. JAVA_HOME, PATH), written by the environment stage."""
    import os
    import subprocess

    script = project / ".law" / "env.sh"
    if not script.is_file():
        return {}
    out = subprocess.run(["bash", "-c", 'set -a; source "$1" >/dev/null 2>&1; env -0', "_", str(script)],
                         capture_output=True, timeout=30, cwd=project)
    if out.returncode != 0:
        return {}
    pairs = (item.split("=", 1) for item in out.stdout.decode(errors="replace").split("\0") if "=" in item)
    return {k: v for k, v in pairs if os.environ.get(k) != v and k not in {"_", "SHLVL", "PWD", "OLDPWD"}}


def build_section(project: Path, version: int) -> str | None:
    """The `## Build version N` section of .law/PLAN.md (the development milestones), or None."""
    plan = project / ".law" / "PLAN.md"
    text = plan.read_text() if plan.exists() else ""
    head = re.search(rf"^(#+)\s*Build version {version}\b.*$", text, re.IGNORECASE | re.MULTILINE)
    if not head:
        return None
    level = len(head.group(1))
    nxt = re.compile(rf"^#{{1,{level}}}\s", re.MULTILINE).search(text, head.end())
    return text[head.end():nxt.start() if nxt else len(text)]


def plan_complete(project: Path, version: int) -> bool:
    """True when this version's build section exists and every milestone in it is ticked. Other checklists in
    PLAN.md (a proposal's sections, a research plan, another version) do not count."""
    section = build_section(project, version)
    return section is not None and "- [x]" in section.lower() and "- [ ]" not in section


def describe_opencode_line(line: str) -> str | None:
    """Turn one `opencode run --format json` event into a short, readable progress line (or None to skip)."""
    if not line.startswith("{"):
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    part = ev.get("part") or {}
    if part.get("type") != "tool" or (part.get("state") or {}).get("status") != "completed":
        return None
    tool = str(part.get("tool", ""))
    args = (part.get("state") or {}).get("input") or {}
    target = args.get("filePath") or args.get("command") or args.get("pattern") or args.get("path") or args.get("url")
    if tool.startswith("mac_"):
        return f"🖥 {tool[4:].replace('_', ' ')}"
    if tool == "webfetch" and target:  # the site only, so chat apps don't render the URL as a link card
        host = re.sub(r"^\w+://", "", str(target)).split("/", 1)[0]
        return f"🌐 reading {'web search' if host.startswith(('127.0.0.1', 'localhost')) else host}"
    label = TOOL_ICONS.get(tool, f"🔧 {tool}")
    if not target:
        return label
    lines = [ln.strip() for ln in str(target).replace(str(Path.home()), "~").splitlines() if ln.strip()]
    if tool == "bash":  # what ran, not the boilerplate around it (every command starts with sourcing env.sh)
        heredoc = next((i for i, ln in enumerate(lines) if "<<" in ln), None)  # after it: data, not commands
        lines = lines[:heredoc + 1] if heredoc is not None else lines
        meaningful = [BANNER.sub("", BOILERPLATE_PREFIX.sub("", ln)).strip() for ln in lines
                      if not BOILERPLATE_LINE.match(ln)]
        lines = [ln for ln in meaningful if ln] or lines
        return f"{label} {' ; '.join(lines)[:140]}"
    text = lines[0] if lines else ""
    return f"{label} {text[:140]}"


BOILERPLATE_LINE = re.compile(r"^(?:source \S*env\.sh|cd \S+|echo [\"']?=+[^;&|]*[\"']?)\s*;?\s*$")
BANNER = re.compile(r"echo\s+([\"'])=+[^\"']*\1\s*(?:;|&&)\s*")  # echo "=== X ===";  between commands
BOILERPLATE_PREFIX = re.compile(r"^(?:(?:source \S*env\.sh|cd \S+)\s*(?:&&|;)\s*)+")


# An agent that changes no file for this long is stuck (seen: a 2h20m re-read loop across context compactions).
# It is stopped and its session resumed with a nudge to build and fix, at most STALL_NUDGES times.
STALL_MINUTES = 25
EMPTY_REVIEW_CHARS = 400  # a review shorter than this has no findings to state a verdict from
STALL_NUDGES = 3
NUDGE = ("You have not changed any file for {minutes} minutes. Stop reading and exploring. If the plan or the "
         "project does not exist yet, write it now: the build section of .law/PLAN.md, then the first ticket's "
         "files. Otherwise run the project's build or tests now, fix the first error they report (open only the "
         "file it names), and repeat. "
         "If the error is a library class or method that does not exist (cannot find symbol, does not override), "
         "run `law api <Class> [member]` once for the real signatures instead of guessing again. "
         "Commit when a gate passes.")
# Writers, researchers and designers produce documents: the nudge is to save work, a section at a time.
NUDGE_DOCS = ("You have not saved anything for {minutes} minutes. Save your work now: write the next section of the "
              "document to its file (at most about 1,500 words per write, appending section by section), tick it in "
              ".law/PLAN.md, and continue the same way. Do not write the whole document in one step.")
# Reading library internals (decompiling jars, hunting jars on disk) to learn an API: seen in four coder runs,
# costing hours each. The sanctioned way is one `law api` call (law.apilookup). After LIB_DIG_LIMIT such commands
# in one step the agent is stopped and resumed with LIB_NUDGE.
LIB_DIG_LIMIT = 3


def is_library_dig(command: str) -> bool:
    """A command that inspects library internals: decompilers, jar listings, a search for library jars anywhere,
    the Maven/Gradle caches' jars, or reading node_modules, the Go module cache or the Xcode SDK. The project's
    own build output (target/, build/) is not a library."""
    c = command.lower()
    if re.search(r"\bjavap\b|\bjar\s+-?t|\bcfr\b|\bprocyon\b|\bfernflower\b", c):
        return True
    # `.ja` at the very end: the progress summary cuts commands at 140 characters.
    if re.search(r"\bfind\b[^|;&]*-i?name\s+\S*\.ja(?:r\b|\S?$)", c) or "localrepository" in c:
        return True
    # Reading installed packages or the SDK to learn an API (cleanup such as `rm -rf node_modules` is not reading).
    reads = re.search(r"(?:^|[\s;&|(])(?:find|grep|rg|cat|head|tail|less|sed|awk|ls)\s", c)
    internals = re.search(r"node_modules/|/pkg/mod/|gomodcache|\.swiftinterface|\.platform/developer/sdks/|"
                          r"\.build/checkouts/", c)
    if reads and internals and not re.search(r"node_modules/\.bin/", c):
        return True
    in_cache = any(k in c for k in (".m2/", "cache/m2", "m2/repository", ".gradle/caches", "cache/gradle"))
    return in_cache and any(k in c for k in (".jar", ".ja\"", "unzip", "find ", "ls ", "javap"))
LIB_NUDGE = ("Stop reading library internals (jars, node_modules, the Go module cache, the Xcode SDK). To learn a "
             "library or SDK API run `law api <name> [member]` in the project (Java, Go, TypeScript/JavaScript, "
             "Swift, Monkey C): it prints the real signatures, or the closest real names when the one you used "
             "does not exist. Use only what it shows, compile, and let the test output guide you. Check "
             ".law/PROGRESS.md for notes, then continue the current ticket.")
# Looking up API after API without writing anything (seen 2026-10-21: LC-3 ran 20+ law api lookups over 1h40m,
# each followed by 10-20 min of planning, and wrote no code). After LOOKUP_LIMIT lookups since the last saved file
# the agent is told to write the code with what it has.
LOOKUP_LIMIT = 6
LOOKUP_NUDGE = ("You have looked up enough APIs. Write the code for the current ticket now, using what the lookups "
                "showed. Then compile and run the tests, and look up only a symbol the compiler reports as wrong. "
                "Small steps: write one file, build, fix, continue.")
DOC_ROLES = {"writer", "researcher", "designer", "analyst", "architect"}


def made_progress(output: str) -> bool:
    """Whether an agent run saved any file before it was stopped."""
    return any(is_file_change(line) for line in output.splitlines())


def is_file_change(line: str) -> bool:
    """Progress for the stall guard: a file written, or a page fetched (research agents read before they write)."""
    summary = describe_opencode_line(line)
    return summary is not None and summary.startswith(("✏️", "🌐"))


def session_id(output: str) -> str | None:
    """The OpenCode session id from `--format json` output (the last one seen)."""
    found = None
    for line in output.splitlines():
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = ev.get("sessionID") or (ev.get("part") or {}).get("sessionID") or found
    return found


def parse_opencode_events(output: str) -> str:
    """Extract the final assistant text from `opencode run --format json` output."""
    texts: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") or {}
        if ev.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
    return texts[-1].strip() if texts else output[-4000:].strip()


def agent_env(rt: Runtime, role: str, effort: str | None = None) -> dict[str, str]:
    """Role config plus XDG dirs so OpenCode keeps its sessions, cache and state under ~/LAW, not ~/.local.
    `effort` picks the role's config with that thinking level (a project's `thinking`), when it exists."""
    base = rt.paths.data / "opencode"
    cache = rt.paths.cache
    config = rt.paths.config / f"opencode-{role}.json"
    if effort and (rt.paths.config / f"opencode-{role}-{effort}.json").exists():
        config = rt.paths.config / f"opencode-{role}-{effort}.json"
    return {"OPENCODE_CONFIG": str(config),
            "XDG_DATA_HOME": str(base / "data"), "XDG_STATE_HOME": str(base / "state"),
            "XDG_CACHE_HOME": str(cache / "opencode"),
            # build-tool caches the agents fill (measured: ~/.m2 and ~/.npm grew during one Java/Angular build)
            "npm_config_cache": str(cache / "npm"), "MAVEN_OPTS": f"-Dmaven.repo.local={cache / 'm2'}",
            "GRADLE_USER_HOME": str(cache / "gradle"), "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv"),
            # LAW's one Colima VM; agent shells skip the login profile, so `colima start` made a second VM in
            # ~/.colima (2026-10-25, Pantry environment stage)
            "COLIMA_HOME": str(rt.paths.home / "colima"),
            # docker talks to that VM even when the shared "colima" context is gone (`colima stop` on another
            # instance removes it)
            "DOCKER_HOST": f"unix://{rt.paths.home / 'colima' / 'default' / 'docker.sock'}"}


def make_handlers(rt: Runtime) -> dict[str, Any]:
    async def search(ctx: Context) -> Outcome:
        hits = await searx(rt, ctx.job.payload["query"], int(ctx.job.payload.get("limit", 8)))
        return Done({"results": hits})

    async def model_generating() -> bool:
        """The model server is producing tokens right now (oMLX's active requests)."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"http://127.0.0.1:{rt.reg.ports.llm_internal}/api/status")
                return int(r.json().get("active_requests", 0)) > 0
        except (httpx.HTTPError, ValueError):
            return False

    async def _agent(ctx: Context, role: str, prompt: str, workdir: Path, *, writes: bool = True,
                     memory: ProjectMemory | None = None) -> str:
        """Run one OpenCode agent. A writing agent that stalls (no file change for STALL_MINUTES) is stopped and
        its session resumed with a nudge to build and fix. With project memory, the session is recorded in
        `.law/session.json`, and a job that restarts (e.g. after a worker restart) resumes that session."""
        rt.roles.get(role)  # validate
        model = f"law/{rt.model_id(role)}"
        stall_min = float(ctx.job.payload.get("stall_min", STALL_MINUTES))
        base = ["opencode", "run", "--agent", role, "-m", model, "--format", "json", "--dir", str(workdir)]
        resumed = memory.session_for(ctx.job.id) if memory else None
        if resumed:
            await ctx.progress(f"{role} agent resuming its interrupted session in {workdir}")
            section = ctx.job.payload.get("section")
            again = (f"Your previous session was interrupted. Finish section \"{section['heading']}\" in "
                     f"{section['file']} (append it if it is missing; do not repeat it if it is there), then stop. "
                     f"LAW tracks the sections: do not edit {FOLDER}/PLAN.md or {FOLDER}/PROGRESS.md.") if section \
                else (memory.resume_prompt() if memory else "")
            cmd = [*base, "--session", resumed, again]
        else:
            await ctx.progress(f"{role} agent working in {workdir}")
            cmd = [*base, prompt]

        started = time.time()
        edits: list[str] = []

        def describe(line: str) -> str | None:
            if memory is not None and line.startswith('{"'):
                sid = session_id(line)
                if sid:
                    memory.save_session(ctx.job.id, role, sid)
            summary = describe_opencode_line(line)
            if summary and summary.startswith("✏️"):
                edits.append(summary.split(" ", 2)[-1])
            return summary

        digs = lookups = 0

        def steer(summary: str) -> str | None:
            nonlocal digs, lookups
            if summary.startswith("✏️"):
                digs = lookups = 0
                return None
            if summary.startswith("▶") and is_library_dig(summary):
                digs += 1
                if digs >= LIB_DIG_LIMIT:
                    digs = 0
                    return LIB_NUDGE
            if summary.startswith("▶"):
                lookups += summary.count("law api ")
                if lookups >= LOOKUP_LIMIT:
                    lookups = 0
                    return LOOKUP_NUDGE
            return None

        # Interventions in a row without progress: an agent that saved files since the last nudge earned a fresh
        # budget (one stage runs a whole milestone for hours; seen 2026-10-21: two tickets committed between
        # nudges, then the fourth nudge failed the pipeline).
        strikes = 0
        while True:
            try:
                env = {**agent_env(rt, role, ctx.job.payload.get("effort")), **project_env(workdir)}
                out = await run_cmd(ctx, cmd, cwd=workdir, env=env, describe=describe,
                                    timeout_s=float(ctx.job.payload.get("timeout_s", 4 * 3600)),
                                    stall_s=stall_min * 60 if writes and stall_min > 0 else None,
                                    is_work=is_file_change, alive=model_generating,
                                    steer=steer if role == "coder" else None)
                answer_text = parse_opencode_events(out)
                if memory is not None and writes and memory.ensure_progress(started, role, answer_text, edits):
                    await ctx.progress("📝 recorded this step in .law/PROGRESS.md (the agent had not)")
                return answer_text
            except Steered as exc:
                sid = session_id(exc.output)
                strikes = 1 if made_progress(exc.output) else strikes + 1
                if sid is None or strikes > STALL_NUDGES:
                    raise Fatal(f"{role} agent kept researching without writing code") from exc
                why = ("looked up enough APIs: telling the agent to write the code" if exc.message == LOOKUP_NUDGE
                       else "reading library internals: redirecting the agent to compile and test instead")
                await ctx.progress(f"⚠️ {why}")
                cmd = [*base, "--session", sid, exc.message]
                continue
            except Stalled as exc:
                sid = session_id(exc.output)
                strikes = 1 if made_progress(exc.output) else strikes + 1
                if sid is None or strikes > STALL_NUDGES:
                    raise Fatal(f"{role} agent stalled: {exc}") from exc
                nudge = NUDGE_DOCS if role in DOC_ROLES else NUDGE
                what = "save the next section" if role in DOC_ROLES else "build and fix"
                await ctx.progress(f"⚠️ nothing saved for {stall_min:.0f} min: nudging the agent to {what}")
                cmd = [*base, "--session", sid, nudge.format(minutes=f"{stall_min:.0f}")]

    def _scoped(p: dict[str, Any], memory: ProjectMemory | None = None, writes: bool = True) -> str:
        """The agent's prompt: the brief (or the raw request), the folder to work in, and how to keep `.law/`."""
        carry = p.get("_carry") or {}
        parts = []
        if _dir(p):  # a named or chosen folder: build there, not in a new one
            parts.append(f"Work in this directory and create the project files directly in it: {_workdir(p)}\n"
                         f"Do not create a different project folder.")
        if memory is not None and p.get("section"):
            # one section of a document LAW assembles: LAW tracks the sections and records progress
            parts.append(f"LAW tracks this document's sections and records progress. Do not edit {FOLDER}/PLAN.md "
                         f"or {FOLDER}/PROGRESS.md; just write the section.")
        elif memory is not None:
            parts.append(memory.instructions())
        elif writes:
            parts.append(NEW_PROJECT_MEMORY)
        parts.append(str(carry.get("brief") or p["prompt"]))
        spec = specs.resolve(p)  # fixed instructions from the pipeline (e.g. /proposal's structure), current version
        if spec:
            parts.append(spec)
        return "\n\n".join(parts)

    def _memory(ctx: Context, p: dict[str, Any]) -> ProjectMemory | None:
        """Project memory lives in the folder the user named; a new project creates its own `.law/`."""
        if not _dir(p):
            return None
        memory = ProjectMemory(_workdir(p))
        memory.prepare(ctx.job.parent or ctx.job.id, str(p["prompt"]), (p.get("_carry") or {}).get("brief"))
        return memory

    def _dir(p: dict[str, Any]) -> str | None:
        """The folder the user named, or the one the translator chose for a new project."""
        return p.get("dir") or (p.get("_carry") or {}).get("dir")

    def _workdir(p: dict[str, Any]) -> Path:
        named = _dir(p)
        workdir = Path(named or rt.paths.projects).expanduser()
        if named and not workdir.exists() and workdir.parent.resolve() == rt.paths.projects.resolve():
            workdir.mkdir()  # `/code ~/SourceCode/new-app ...` starts a new project in that folder
        if not workdir.is_dir():
            raise Fatal(f"directory does not exist: {workdir}")
        return workdir

    async def task(ctx: Context) -> Outcome:
        """Run a role as an OpenCode agent. Code-writing roles get an independent review and a fix pass."""
        p = ctx.job.payload
        role = p.get("role", "coder")
        workdir = _workdir(p)
        translate = p.get("translate", role in REVIEWED_ROLES)
        review = p.get("review", role in REVIEWED_ROLES)
        if (translate or review) and ctx.job.parent is None:
            base = {"prompt": p["prompt"], "role": role, **({"dir": str(workdir)} if p.get("dir") else {})}
            stages = [{"kind": "task_translate", "payload": base}] if translate else []
            stages.append({"kind": "task_work", "payload": base})
            if review:
                stages += [{"kind": "task_review", "payload": base}, {"kind": "task_fix", "payload": base}]
            return Stages(stages)
        writes = role in REVIEWED_ROLES
        memory = _memory(ctx, p) if writes else None
        text = await _agent(ctx, role, _scoped(p, memory, writes), workdir, writes=writes, memory=memory)
        return Done({"answer": text, "dir": str(workdir)})

    async def task_translate(ctx: Context) -> Outcome:
        """The translator turns the request into a precise brief, aware of what the project folder already holds."""
        p = ctx.job.payload
        workdir = _workdir(p)
        named = bool(p.get("dir"))
        choose = not named and p["role"] == "coder"
        root = rt.paths.projects
        projects = sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")) \
            if root.is_dir() else []
        prompt = TRANSLATE.format(
            role=p["role"], request=p["prompt"],
            folder=workdir if named else "a new folder (see below)",
            listing=project_listing(workdir) if named else "(new project)",
            memory=(ProjectMemory(workdir).context() if named else "") or "(none)",
            fixed=FIXED.format(spec=specs.resolve(p)) if specs.resolve(p) else "",
            choose=CHOOSE_FOLDER.format(root=root, projects=", ".join(projects) or "(none)") if choose else "")
        await ctx.progress("📝 translating the request into a brief")
        brief = (await answer(rt, "translator", [{"role": "user", "content": prompt}], ctx)).strip()
        if not brief:
            raise Fatal("the translator returned an empty brief")
        result: dict[str, Any] = {"brief": brief}
        if choose:
            folder = project_folder(brief, root)
            if folder is None:
                raise Fatal("the translator did not name a valid project folder under " + str(root))
            folder.mkdir(parents=True, exist_ok=True)
            result["dir"] = str(folder)
            await ctx.progress(f"📁 project folder: {folder}")
        await ctx.progress(f"📝 brief ready ({len(brief.split())} words)")
        return Done(result)

    async def task_work(ctx: Context) -> Outcome:
        p = ctx.job.payload
        if p.get("milestone") and plan_complete(_workdir(p), int(p.get("version", 1))):
            return Done({"dir": str(_workdir(p))})  # every milestone of this version is ticked
        section = p.get("section")
        if section and section.get("skip_if_written"):
            from law.handlers.proposal import section_written

            doc = _workdir(p) / section["file"]
            if doc.is_file() and section_written(doc.read_text(), section["heading"]):
                return Done({"dir": str(_workdir(p))})  # this section is already in the document
        memory = _memory(ctx, p)
        text = await _agent(ctx, p["role"], _scoped(p, memory), _workdir(p), memory=memory)
        return Done({"answer": text, "dir": str(_workdir(p))})

    async def task_review(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = ctx.job.payload["_carry"]
        if carry.get("passed"):  # an earlier review round already passed
            return Done({})
        report = carry.get("answer", "")
        brief = f"\n\nThe author worked from this brief:\n{carry['brief'][:6000]}" if carry.get("brief") else ""
        rubric = specs.resolve(p, "rubric") or CODE_RUBRIC
        prompt = (f"{rubric}\n\nThe REQUEST was:\n{p['prompt']}{brief}\n\nThe author reported:\n{report[:4000]}\n\n"
                  "Inspect the files the author created or changed (the project may be in a new folder under "
                  "~/SourceCode named after it). Verify claims against the code; do not trust the report.")
        reply = await _agent(ctx, "reviewer", prompt, _workdir(p), writes=False)
        verdict = parse_verdict(reply)
        if verdict.get("unscored") and len(reply.strip()) < EMPTY_REVIEW_CHARS:
            # no findings to restate (seen: a compacted session ended with one line, then a restated 5/5): review again
            await ctx.progress("the review ended without findings: reviewing again")
            reply = await _agent(ctx, "reviewer", prompt + "\n\nRead each file once, note problems as you go, and end "
                                 "with the JSON verdict.", _workdir(p), writes=False)
            verdict = parse_verdict(reply)
        if verdict.get("unscored"):  # ask the same model to state its verdict in the required format
            await ctx.progress("the reviewer gave no score: asking it to state its verdict")
            again = await answer(rt, "reviewer", [{"role": "user", "content": (
                f"Here is your review:\n\n{reply[-6000:]}\n\nState its verdict as JSON only: "
                '{"score": <1-5>, "issues": ["<file or section: problem and fix>"], "summary": "<one sentence>"}')}],
                ctx)
            verdict = parse_verdict(again)
        if carry.get("diagram_errors"):  # checked by drawing them: the reviewer cannot see this
            verdict = {**verdict, "score": min(int(verdict.get("score") or 0), PASS_SCORE - 1),
                       "issues": [*carry["diagram_errors"], *verdict.get("issues", [])][:12]}
        return Done({"review": verdict})

    async def task_fix(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = ctx.job.payload["_carry"]
        verdict = carry["review"]
        if carry.get("passed"):
            return Done({})
        if verdict["score"] >= PASS_SCORE:
            return Done({"answer": carry.get("answer", ""), "review": verdict, "fixed": False, "passed": True,
                         "dir": carry.get("dir")})
        issues = "\n".join(f"- {i}" for i in verdict["issues"]) or "- raise the quality to client-ready"
        prompt = (f"An independent reviewer scored your work {verdict['score']}/5 for this request:\n{p['prompt']}\n\n"
                  f"Fix every issue below, then rerun the project's checks until they pass and report the output:\n"
                  f"{issues}")
        spec = specs.resolve(p)
        if spec:
            prompt += f"\n\nThe work must still meet these instructions:\n{spec}"
        memory = _memory(ctx, p)
        if memory is not None:
            prompt = f"{memory.instructions()}\n\n{prompt}"
        text = await _agent(ctx, p["role"], prompt, _workdir(p), memory=memory)
        return Done({"answer": text, "review": verdict, "fixed": True, "passed": False, "dir": carry.get("dir")})

    return {"search": search, "task": task, "task_translate": task_translate, "task_work": task_work,
            "task_review": task_review, "task_fix": task_fix}
