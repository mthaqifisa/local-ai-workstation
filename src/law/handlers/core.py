"""Text handlers: ask, judge and revise (the quality loop)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from law import calc
from law.handlers.base import Runtime
from law.llm import TruncatedError, chat_with_tools, image_part
from law.paths import LawPaths, display
from law.queue.store import JobStore
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

PASS_SCORE = 4

# What the chat can hand to an agent (start_agent): the model names the deliverable, LAW picks the agent. Naming the
# deliverable is the easier call: asked for an agent, the model sent "study this code and write a PDF report" to the
# coder 2 times in 6 (2026-10-24 probe), once in LAW's own repo.
DELIVERABLES = {
    "document": ("writer", "a report, document, PDF, proposal, memo or slides, also about code or a project"),
    "software": ("coder", "new or changed software: build, fix, refactor or test code"),
    "research": ("researcher", "web research answered with sources"),
    "data": ("analyst", "calculations, data analysis, benchmarks or charts"),
    "design": ("designer", "UI/UX design or visual critique"),
    "architecture": ("architect", "system design, technology choices or ADRs"),
    "mac_task": ("operator", "something done in apps on this Mac: music, settings, downloads, browsing"),
}
REVISE_ACTIONS = {"awaiting_go": "nogo", "awaiting_verification": "changes", "done": "changes", "parked": "resume"}
OWNER_WORDS = "\n\nThe owner's exact words (they win over anything above that differs):\n{words}"
PROJECT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "start_project",
        "description": ("Start a new app or product as a LAW project, which appears on the Console's project page. "
                        "It stops for the user's go before any code. Returns the job id."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The project's name, as the user gave it."},
                "summary": {"type": "string", "description": "One sentence: what it is."},
                "details": {"type": "string", "description": "Everything the user asked for, with every detail."},
                "stack": {"type": "string", "description": "The technology, if the user or the chat settled it."},
                "folder": {"type": "string",
                           "description": ("Only if the user named one: the folder under ~/SourceCode, e.g. "
                                           "RnD/digital-bank. Leave empty to derive it from the name.")},
                "mode": {"type": "string", "enum": ["code", "project"],
                         "description": ("code: a short plan and tickets for the user's go, no documents; project: "
                                         "research and a proposal PDF first, for a full engagement.")},
            },
            "required": ["name", "summary", "details"],
        },
    },
}
REVISE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "revise_project",
        "description": ("Redo or change a project LAW already has: its agents write a new version of the plan or "
                        "proposal and new tickets, which replace the old ones on the Console, then it stops for the "
                        "user's go. During a build, the working agent gets the change and updates its tickets."),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "The project's name or folder name."},
                "changes": {"type": "string",
                            "description": "What the user wants redone or changed, with every detail."},
                "proposal": {"type": "boolean", "description": "true when the user asks for a proposal document."},
            },
            "required": ["project", "changes"],
        },
    },
}
SEND_FILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "send_file",
        "description": ("Attach an existing file to your reply in this chat (a memo, report, PDF, image or audio an "
                        "agent made, as named in the conversation). Use it when the user asks for a file; never "
                        "start an agent just to send one."),
        "parameters": {"type": "object", "properties": {"path": {"type": "string",
                                                                  "description": "Absolute path of the file"}},
                       "required": ["path"]},
    },
}
SEND_MAX_BYTES = 45 * 1024 * 1024  # Telegram bots may send up to 50 MB


def attach(paths: LawPaths, raw: str, files: list[str]) -> str:
    """`send_file`: check the path and queue it for the reply."""
    f = Path(raw).expanduser()
    if not f.is_absolute() or not f.resolve().is_relative_to(paths.user_home.resolve()):
        return f"error: give an absolute path in {display(paths.user_home)}"
    if not f.is_file():
        return f"error: no such file: {display(f)}"
    if f.stat().st_size > SEND_MAX_BYTES:
        return "error: the file is over 45 MB, too big for Telegram"
    if str(f) not in files:
        files.append(str(f))
    return f"attached {f.name}; it is sent with your reply"


AGENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "start_agent",
        "description": ("Hand work to an agent that can read and write files, run commands, browse the web, make "
                        "documents and PDFs, and send the results by Telegram or email. Use it for any request "
                        "that needs more than a direct answer. Returns the job id; the agent reports in this chat."),
        "parameters": {
            "type": "object",
            "properties": {
                "deliverable": {"type": "string", "enum": list(DELIVERABLES),
                                "description": "What the user gets at the end. " + "; ".join(
                                    f"{k}: {v[1]}" for k, v in DELIVERABLES.items())},
                "request": {"type": "string",
                            "description": ("The complete task, keeping every detail the user gave (paths, file "
                                            "formats, recipients, wording, quality bar) and what the conversation "
                                            "settled since.")},
                "folder": {"type": "string",
                           "description": ("Absolute path of the folder the user named for the results (\"store "
                                           "all documents in X\" -> X), or the project folder for software.")},
            },
            "required": ["deliverable", "request"],
        },
    },
}
ACTIVE_LOOKBACK = 1000  # recent jobs searched for a live agent: a long pipeline is followed by many stage jobs
MIN_WORDS = 20  # shorter messages ("yes", "go on") add nothing to a brief
HISTORY_CHARS = 8000  # conversation the judge sees as context

RUBRIC = """Score the RESPONSE to the REQUEST from 1 to 5.
5 = correct, complete, well structured, ready for a demanding client
4 = correct and useful, cosmetic issues only
3 = mostly right with one material gap or error
2 = significant errors or omissions
1 = wrong or unusable
Check correctness, whether it answers the request, structure, clarity and invented facts.
Recompute every number, rate and total with the run_python tool before scoring; a wrong figure caps the score at 2.
Run self-contained code in the RESPONSE with run_python, with its tests and the edge cases the request implies; code
that fails, or run output and test results the response shows that your run does not reproduce, cap the score at 2.
Reply with JSON only: {"score": <1-5>, "issues": ["<problem and fix>"], "summary": "<one sentence>"}"""


PROSE_SCORE = re.compile(r"\bscore\b[^0-9\n]{0,12}([0-5])(?:\s*/\s*5)?\b|\b([0-5])\s*/\s*5\b", re.IGNORECASE)


def prose_score(text: str) -> int | None:
    """A judge that answered in prose ("board-ready (Score: 5)", "4/5"): its last stated score, if any."""
    found = [int(a or b) for a, b in PROSE_SCORE.findall(text)]
    return found[-1] if found else None


def parse_verdict(text: str) -> dict[str, Any]:
    """The judge's JSON verdict. A reply in prose with a clear score is accepted (seen: a 5/5 'board-ready' verdict
    without JSON was read as 0 and sent a finished document into a needless fix round); with no score at all it is
    unparseable (`unscored`, score 0) so the caller can ask again."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    data: dict[str, Any] | None = None
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = None
    if data is None:
        score = prose_score(text)
        if score is None:
            return {"score": 0, "issues": ["the reviewer gave no score"], "summary": text.strip()[:300],
                    "unscored": True}
        return {"score": score, "issues": [], "summary": text.strip()[:300], "format": "prose"}
    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return {"score": max(0, min(5, score)), "issues": [str(i) for i in data.get("issues", [])][:10],
            "summary": str(data.get("summary", ""))}


def build_messages(rt: Runtime, role: str, prompt: str, history: list[dict[str, Any]] | None = None,
                   images: list[str] | None = None) -> list[dict[str, Any]]:
    """System prompt + history + the user turn; images become vision content parts."""
    content: str | list[dict[str, Any]] = prompt
    if images:
        content = [{"type": "text", "text": prompt}, *(image_part(Path(i)) for i in images)]
    return [{"role": "system", "content": rt.roles.system_prompt(role)}, *(history or []),
            {"role": "user", "content": content}]


def _uv_env(paths: LawPaths) -> dict[str, str]:
    """uv's caches inside ~/LAW, as the installer sets them."""
    return {**os.environ, "UV_CACHE_DIR": str(paths.cache / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(paths.runtime / "python")}


PROJECTS_SHOWN = 12


def projects_card(paths: LawPaths) -> str:
    """Every project's name, folder, state and last event, read fresh from LAW's records for each message: Tony
    knows which project the user means without relying on the chat history (which keeps only 16 messages)."""
    from law.projects import Projects  # here, as in the tools below: law.projects imports the handlers' modules

    rows = sorted(Projects(paths).all(), key=lambda p: p.history[-1].at if p.history else "", reverse=True)
    if not rows:
        return ""
    lines = [f"- {p.name} (id {p.id}, folder {display(Path(p.dir))}): {p.state.replace('_', ' ')}"
             + (f"; last: {p.history[-1].event} {p.history[-1].at[:16].replace('T', ' ')}" if p.history else "")
             + (f"; latest progress note: {_latest_progress(Path(p.dir))}" if _latest_progress(Path(p.dir)) else "")
             for p in rows[:PROJECTS_SHOWN]]
    return ("\n\nPROJECTS (current, from LAW's records; newest activity first):\n" + "\n".join(lines))


def _latest_progress(folder: Path) -> str:
    """The heading of the newest entry in the project's .law/PROGRESS.md (agents append one per step)."""
    try:
        heads = [x for x in (folder / ".law" / "PROGRESS.md").read_text(errors="replace").splitlines()
                 if x.startswith("## ")]
    except OSError:
        return ""
    return heads[-1][3:].strip()[:160] if heads else ""


def with_facts(messages: list[dict[str, Any]], facts: str) -> list[dict[str, Any]]:
    """The system prompt plus facts read from LAW's records (the projects list)."""
    return [{**messages[0], "content": messages[0]["content"] + facts}, *messages[1:]] if facts else messages


def _images(payload: dict[str, Any]) -> list[str]:
    imgs = payload.get("images") or ([payload["image"]] if payload.get("image") else [])
    missing = [i for i in imgs if not Path(i).is_file()]
    if missing:
        raise Fatal(f"image not found: {missing[0]}")
    return list(imgs)


LOWER = {"xhigh": "medium", "medium": "low", "low": "low", "off": "off"}


AgentStarter = Callable[[dict[str, Any]], Awaitable[str]]


async def answer(rt: Runtime, role: str, messages: list[dict[str, Any]], ctx: Context | None = None, *,
                 model: str | None = None, agents: AgentStarter | None = None,
                 files: list[str] | None = None) -> str:
    """Ask the role's model (or `model`, e.g. the one a shared slot runs on) at the role's reasoning effort, with
    the calculator if the role has it and `start_agent` when `agents` is given.
    If the model thinks itself out of tokens, retry one step lower."""
    r = rt.roles.get(role)
    model_id = model or rt.model_id(role)
    on_thought = ctx.thought if ctx is not None else None
    tools = [calc.TOOL_SPEC] if "python" in r.tools else []
    tools += [AGENT_TOOL, PROJECT_TOOL, REVISE_TOOL] if agents is not None else []
    tools += [SEND_FILE_TOOL] if files is not None else []

    async def run_tool(name: str, args: dict[str, Any]) -> str:
        if name == "run_python" and "python" in r.tools:
            python, code = rt.paths.runtime / "calc" / "bin" / "python", str(args.get("code", ""))
            out = await calc.run_python(python, rt.paths.tmp / "calc", code)
            module = calc.missing_module(out)
            if module:  # self-healing: add the missing package, say so, run again
                pinned = await calc.install_missing(python, module, _uv_env(rt.paths),
                                                    rt.paths.state / "calc-extra.txt")
                if ctx is not None:
                    await ctx.progress(f"🔧 installed {pinned} for {role}'s Python" if pinned
                                       else f"🔧 could not install the package for module {module}")
                if pinned:
                    out = await calc.run_python(python, rt.paths.tmp / "calc", code)
            if ctx is not None:  # visible in the job log, so a claimed run can be checked against a real one
                await ctx.progress(f"🧮 {role} ran Python: {out.splitlines()[-1][:120] if out else 'no output'}")
            return out
        if name == "send_file" and files is not None:
            return attach(rt.paths, str(args.get("path", "")), files)
        if name in {"start_agent", "start_project", "revise_project"} and agents is not None:
            return await agents({**args, "_tool": name})
        return f"error: unknown tool {name}"

    async def ask_at(effort: str) -> str:
        sampling = sampling_body(rt, model_id, effort)
        if tools:
            return await chat_with_tools(rt.llm, model_id, messages, tools, run_tool, temperature=r.temperature,
                                         reasoning=effort, on_thought=on_thought, extra=sampling)
        return await rt.llm.chat(model_id, messages, temperature=r.temperature, reasoning=effort,
                                 on_thought=on_thought, extra=sampling)

    try:
        return await ask_at(r.reasoning)
    except TruncatedError:
        return await ask_at(LOWER[r.reasoning])


def sampling_body(rt: Runtime, model_id: str, effort: str | None) -> dict[str, Any]:
    """The model card's sampling for this model at this thinking level (it replaces the role's temperature), or
    nothing for a model without one."""
    try:
        sampling = rt.reg.model(model_id).sampling_for(effort)
    except KeyError:
        return {}
    return sampling.body() if sampling else {}


def conversation_text(history: list[dict[str, Any]] | None, limit: int = HISTORY_CHARS) -> str:
    """The recent conversation as plain text for the judge (who sees no chat turns), newest kept."""
    lines = [f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
             for m in history or [] if isinstance(m.get("content"), str)]
    text = "\n\n".join(lines)[-limit:]
    return f"EARLIER CONVERSATION (context only; score the RESPONSE to the last REQUEST):\n{text}\n\n" if text else ""


def job_model(rt: Runtime, ctx: Context, role: str) -> str:
    """The language model this job was scheduled on: the role's, or the loaded one in a shared slot."""
    resource = ctx.job.resource
    return resource.removeprefix("llm:") if resource.startswith("llm:") else rt.model_id(role)


def review_skip(rt: Runtime, ctx: Context, role: str, text: str, agent_job: str | None) -> str | None:
    """Why this chat answer goes out without the independent review (ADR-0017), or None to review it."""
    if agent_job:
        return "handed to an agent (its work gets its own review)"
    quick = rt.roles.get(role).quick_reply_chars
    if quick and len(text) <= quick and not _images(ctx.job.payload):
        return "short reply"
    if job_model(rt, ctx, role) != rt.model_id(role):
        return "answered beside a running agent step; the reviewer model is not loaded"
    return None


async def _agent_in(store: JobStore, folder: Path) -> str | None:
    """A live agent task on this folder. Two pipelines take turns on the heavy lane, so a second one would work in
    the same tree between the first one's stages (2026-11-01: a message sent twice queued two Pantry coders)."""
    for job in await store.history(ACTIVE_LOOKBACK):
        where = job.payload.get("dir")
        if job.parent is None and job.kind == "task" and not job.status.terminal and where:
            try:
                if Path(where).samefile(folder):  # the chat model's spelling may differ: Pantry, pantry
                    return job.id
            except OSError:
                continue
    return None


def make_handlers(rt: Runtime) -> dict[str, Any]:
    def agent_starter(ctx: Context, started: list[str]) -> AgentStarter:
        """`start_agent` for this chat message: queues one agent task that reports to the same chat."""
        async def start_project(args: dict[str, Any], chat: int | None, reply: dict[str, Any] | None) -> str:
            from law.app import resolver
            from law.projects import Projects, TemplateError, parse_template, project_id
            from law.queue.jobs import Job

            summary = " ".join(str(args.get("summary", "")).split())
            mode = "project" if args.get("mode") == "project" else "code"
            folder = str(args.get("folder") or "").strip()
            template = (f"name: {args.get('name', '')}\nstack: {args.get('stack', '')}\nsummary: {summary}\n"
                        + (f"repo: {folder}\n" if folder else "")  # before details, which takes the rest
                        + f"details: {args.get('details', '')}")
            try:
                fields = parse_template(template)
            except TemplateError as exc:
                return f"error: {exc}"
            if any(p.id == project_id(fields["repo"]) for p in Projects(rt.paths).all()):
                return f"error: project {fields['repo']} already exists; use revise_project to redo it"
            job = await ctx.store.submit(resolver(rt)(Job(
                kind="project_new", payload={"fields": fields, "chat": chat, "mode": mode}, source=ctx.job.source,
                reply_to=reply)))
            started.append(job.id)
            await ctx.progress(f"🚀 started project {fields['name']}: job {job.id}")
            return f"started project job {job.id}: it writes the plan and tickets, then waits for the user's go"

        async def revise_project(args: dict[str, Any], chat: int | None, reply: dict[str, Any] | None) -> str:
            from law import board
            from law.app import resolver
            from law.project_memory import ProjectMemory
            from law.projects import Projects
            from law.queue.jobs import Job

            changes = str(args.get("changes", "")).strip()
            if not changes:
                return "error: changes is empty"
            try:
                project = Projects(rt.paths).get(str(args.get("project", "")))
            except KeyError as exc:
                return f"error: {str(exc).strip(chr(39) + chr(34))}"
            words = str(ctx.job.payload.get("prompt", "")).strip()
            if len(words) >= MIN_WORDS and words not in changes:
                changes += OWNER_WORDS.format(words=words)
            action = REVISE_ACTIONS.get(project.state)
            if action is None:  # agents are at work on it: the one on it changes its own tickets at its next action
                ProjectMemory(Path(project.dir)).add_note(changes)
                board.add_note(rt.paths, project.id, changes, author="owner (Telegram)")
                return (f"noted: the agent working on {project.name} gets this at its next action and updates its "
                        "tickets; no new job was started")
            job = await ctx.store.submit(resolver(rt)(Job(
                kind="project_step", source=ctx.job.source, reply_to=reply,
                payload={"project": project.id, "action": action, "note": changes, "chat": chat,
                         "proposal": bool(args.get("proposal"))})))
            started.append(job.id)
            await ctx.progress(f"🚀 redoing {project.name}: job {job.id}")
            return (f"started job {job.id}: agents write version {project.version + 1} and its tickets, then wait for "
                    "the user's go")

        async def start(args: dict[str, Any]) -> str:
            from law.app import resolver
            from law.queue.jobs import Job

            if started:  # one agent per message; a retried model call must not start a second one
                return f"already started job {started[0]} for this message"
            chat = (ctx.job.reply_to or {}).get("chat_id")
            reply = {"chat_id": chat, "status_id": None, "file_field": None, "then": None} if chat else None
            if args.get("_tool") == "start_project":
                return await start_project(args, chat, reply)
            if args.get("_tool") == "revise_project":
                return await revise_project(args, chat, reply)
            kind = str(args.get("deliverable", "")).strip()
            request = str(args.get("request", "")).strip()
            if kind not in DELIVERABLES:
                return f"error: deliverable must be one of {', '.join(DELIVERABLES)}"
            role = DELIVERABLES[kind][0]
            if not request:
                return "error: request is empty"
            words = str(ctx.job.payload.get("prompt", "")).strip()
            if len(words) >= MIN_WORDS and words not in request:  # the chat model adds guesses: keep the source
                request += OWNER_WORDS.format(words=words)
            payload: dict[str, Any] = {"role": role, "prompt": request}
            folder = str(args.get("folder") or "").strip()
            if folder:
                path = Path(folder).expanduser()
                if not path.is_absolute() or not path.resolve().is_relative_to(rt.paths.user_home.resolve()):
                    return f"error: folder must be an absolute path in {rt.paths.user_home}"
                path.mkdir(parents=True, exist_ok=True)
                busy = await _agent_in(ctx.store, path)
                if busy:
                    return (f"not started: agent job {busy} is already working in {path}. Tell the user that job has "
                            "this folder; anything new for it can be sent once it finishes")
                payload["dir"] = str(path)
            elif role != "coder":  # a coder's brief names its project folder; other work gets its own folder
                path = rt.paths.outputs / "agents" / ctx.job.id
                path.mkdir(parents=True, exist_ok=True)
                payload["dir"] = str(path)
            job = await ctx.store.submit(resolver(rt)(Job(kind="task", payload=payload, source=ctx.job.source,
                                                          reply_to=reply)))
            started.append(job.id)
            await ctx.progress(f"🚀 started the {role} agent: job {job.id}")
            where = "progress and the result will be posted in this chat" if chat else "follow it with /jobs"
            return f"started job {job.id} ({role} agent, folder {payload.get('dir', 'chosen by the brief')}); {where}"
        return start

    async def ask(ctx: Context) -> Outcome:
        p = ctx.job.payload
        role = p.get("role", "assistant")
        images = _images(p)
        model = job_model(rt, ctx, role)
        if images and not rt.reg.model(model).vision:
            raise Fatal(f"the {role} role's model cannot see images; use assistant, analyst or designer")
        history = p.get("history") or []
        root = ctx.job.parent is None
        started: list[str] = []
        agents = agent_starter(ctx, started) if root and "agents" in rt.roles.get(role).tools else None
        await ctx.progress(f"{role} is answering")
        # Tony: which projects exist and where each stands, fresh for every message
        facts = projects_card(rt.paths) if agents is not None else ""
        messages = with_facts(build_messages(rt, role, p["prompt"], history, images), facts)
        files: list[str] = []
        text = await answer(rt, role, messages, ctx, model=model, agents=agents,
                            files=files if agents is not None else None)
        agent_job: dict[str, Any] = {"agent_job": started[0]} if started else {}
        if files:
            agent_job["files"] = files
        if not (p.get("quality", True) and root):
            return Done({"answer": text, **agent_job})
        skip = review_skip(rt, ctx, role, text, started[0] if started else None)
        if skip:
            return Done({"answer": text, "revised": False, "review": f"skipped: {skip}", **agent_job})
        # the draft is written; the independent review (another model family) and a revision only if it is weak
        seen = {"images": images} if images else {}
        return Stages([
            {"kind": "judge", "payload": {"prompt": p["prompt"], "history": history, "facts": facts}},
            {"kind": "revise", "payload": {"role": role, "prompt": p["prompt"], "history": history, "facts": facts,
                                           **seen}},
        ], carry={"answer": text, **({"files": files} if files else {})})

    async def draft(ctx: Context) -> Outcome:
        return await ask(ctx)

    async def judge(ctx: Context) -> Outcome:
        carry = ctx.job.payload["_carry"]
        await ctx.progress("independent review")
        context = conversation_text(ctx.job.payload.get("history"))
        facts = ctx.job.payload.get("facts") or ""
        if facts:  # the reviewer must know what the assistant was told, or it scores true facts as invented
            context += f"FACTS THE ASSISTANT WAS GIVEN (from LAW's own records; true, not invented):{facts}\n\n"
        text = await answer(rt, "reviewer", build_messages(
            rt, "reviewer",
            f"{RUBRIC}\n\n{context}REQUEST:\n{ctx.job.payload['prompt']}\n\nRESPONSE:\n{carry['answer']}"), ctx)
        return Done({"verdict": parse_verdict(text)})

    async def revise(ctx: Context) -> Outcome:
        carry = ctx.job.payload["_carry"]
        verdict = carry["verdict"]
        if verdict["score"] >= PASS_SCORE:
            return Done({"answer": carry["answer"], "verdict": verdict, "revised": False,
                         **({"files": carry["files"]} if carry.get("files") else {})})
        role = ctx.job.payload["role"]
        await ctx.progress(f"revising (review score {verdict['score']}/5)")
        feedback = "\n".join(f"- {i}" for i in verdict["issues"]) or "- improve correctness and completeness"
        prompt = (f"Request:\n{ctx.job.payload['prompt']}\n\nYour previous answer:\n{carry['answer']}\n\n"
                  f"A reviewer found these problems:\n{feedback}\n\nWrite the improved, complete answer. The user saw "
                  "neither the previous answer nor the review: answer the request directly as your only reply, and "
                  "do not mention a previous version, a correction, the reviewer or these problems.")
        messages = with_facts(build_messages(rt, role, prompt, ctx.job.payload.get("history"),
                                             _images(ctx.job.payload)), ctx.job.payload.get("facts") or "")
        text = await answer(rt, role, messages, ctx, model=job_model(rt, ctx, role))
        return Done({"answer": text, "verdict": verdict, "revised": True,
                     **({"files": carry["files"]} if carry.get("files") else {})})

    return {"ask": ask, "draft": draft, "judge": judge, "revise": revise}
