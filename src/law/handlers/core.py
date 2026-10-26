"""Text handlers: ask, judge and revise (the quality loop)."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from law import calc
from law.handlers.base import Runtime
from law.llm import TruncatedError, chat_with_tools, image_part
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
HISTORY_CHARS = 8000  # conversation the judge sees as context

RUBRIC = """Score the RESPONSE to the REQUEST from 1 to 5.
5 = correct, complete, well structured, ready for a demanding client
4 = correct and useful, cosmetic issues only
3 = mostly right with one material gap or error
2 = significant errors or omissions
1 = wrong or unusable
Check correctness, whether it answers the request, structure, clarity and invented facts.
Recompute every number, rate and total with the run_python tool before scoring; a wrong figure caps the score at 2.
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


def _images(payload: dict[str, Any]) -> list[str]:
    imgs = payload.get("images") or ([payload["image"]] if payload.get("image") else [])
    missing = [i for i in imgs if not Path(i).is_file()]
    if missing:
        raise Fatal(f"image not found: {missing[0]}")
    return list(imgs)


LOWER = {"xhigh": "medium", "medium": "low", "low": "low", "off": "off"}


AgentStarter = Callable[[dict[str, Any]], Awaitable[str]]


async def answer(rt: Runtime, role: str, messages: list[dict[str, Any]], ctx: Context | None = None, *,
                 model: str | None = None, agents: AgentStarter | None = None) -> str:
    """Ask the role's model (or `model`, e.g. the one a shared slot runs on) at the role's reasoning effort, with
    the calculator if the role has it and `start_agent` when `agents` is given.
    If the model thinks itself out of tokens, retry one step lower."""
    r = rt.roles.get(role)
    model_id = model or rt.model_id(role)
    on_thought = ctx.thought if ctx is not None else None
    tools = ([calc.TOOL_SPEC] if "python" in r.tools else []) + ([AGENT_TOOL] if agents is not None else [])

    async def run_tool(name: str, args: dict[str, Any]) -> str:
        if name == "run_python" and "python" in r.tools:
            return await calc.run_python(rt.paths.runtime / "calc" / "bin" / "python", rt.paths.tmp / "calc",
                                         str(args.get("code", "")))
        if name == "start_agent" and agents is not None:
            return await agents(args)
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


def make_handlers(rt: Runtime) -> dict[str, Any]:
    def agent_starter(ctx: Context, started: list[str]) -> AgentStarter:
        """`start_agent` for this chat message: queues one agent task that reports to the same chat."""
        async def start(args: dict[str, Any]) -> str:
            from law.app import resolver
            from law.queue.jobs import Job

            if started:  # one agent per message; a retried model call must not start a second one
                return f"already started job {started[0]} for this message"
            kind = str(args.get("deliverable", "")).strip()
            request = str(args.get("request", "")).strip()
            if kind not in DELIVERABLES:
                return f"error: deliverable must be one of {', '.join(DELIVERABLES)}"
            role = DELIVERABLES[kind][0]
            if not request:
                return "error: request is empty"
            payload: dict[str, Any] = {"role": role, "prompt": request}
            folder = str(args.get("folder") or "").strip()
            if folder:
                path = Path(folder).expanduser()
                if not path.is_absolute() or not path.resolve().is_relative_to(rt.paths.user_home.resolve()):
                    return f"error: folder must be an absolute path in {rt.paths.user_home}"
                path.mkdir(parents=True, exist_ok=True)
                payload["dir"] = str(path)
            elif role != "coder":  # a coder's brief names its project folder; other work gets its own folder
                path = rt.paths.outputs / "agents" / ctx.job.id
                path.mkdir(parents=True, exist_ok=True)
                payload["dir"] = str(path)
            chat = (ctx.job.reply_to or {}).get("chat_id")
            reply = {"chat_id": chat, "status_id": None, "file_field": None, "then": None} if chat else None
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
        text = await answer(rt, role, build_messages(rt, role, p["prompt"], history, images), ctx,
                            model=model, agents=agents)
        agent_job = {"agent_job": started[0]} if started else {}
        if not (p.get("quality", True) and root):
            return Done({"answer": text, **agent_job})
        skip = review_skip(rt, ctx, role, text, started[0] if started else None)
        if skip:
            return Done({"answer": text, "revised": False, "review": f"skipped: {skip}", **agent_job})
        # the draft is written; the independent review (another model family) and a revision only if it is weak
        seen = {"images": images} if images else {}
        return Stages([
            {"kind": "judge", "payload": {"prompt": p["prompt"], "history": history}},
            {"kind": "revise", "payload": {"role": role, "prompt": p["prompt"], "history": history, **seen}},
        ], carry={"answer": text})

    async def draft(ctx: Context) -> Outcome:
        return await ask(ctx)

    async def judge(ctx: Context) -> Outcome:
        carry = ctx.job.payload["_carry"]
        await ctx.progress("independent review")
        context = conversation_text(ctx.job.payload.get("history"))
        text = await answer(rt, "reviewer", build_messages(
            rt, "reviewer",
            f"{RUBRIC}\n\n{context}REQUEST:\n{ctx.job.payload['prompt']}\n\nRESPONSE:\n{carry['answer']}"), ctx)
        return Done({"verdict": parse_verdict(text)})

    async def revise(ctx: Context) -> Outcome:
        carry = ctx.job.payload["_carry"]
        verdict = carry["verdict"]
        if verdict["score"] >= PASS_SCORE:
            return Done({"answer": carry["answer"], "verdict": verdict, "revised": False})
        role = ctx.job.payload["role"]
        await ctx.progress(f"revising (review score {verdict['score']}/5)")
        feedback = "\n".join(f"- {i}" for i in verdict["issues"]) or "- improve correctness and completeness"
        prompt = (f"Request:\n{ctx.job.payload['prompt']}\n\nYour previous answer:\n{carry['answer']}\n\n"
                  f"A reviewer found these problems:\n{feedback}\n\nWrite the improved, complete answer.")
        text = await answer(rt, role, build_messages(rt, role, prompt, ctx.job.payload.get("history"),
                                                     _images(ctx.job.payload)), ctx, model=job_model(rt, ctx, role))
        return Done({"answer": text, "verdict": verdict, "revised": True})

    return {"ask": ask, "draft": draft, "judge": judge, "revise": revise}
