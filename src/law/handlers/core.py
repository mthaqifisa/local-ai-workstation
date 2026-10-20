"""Text handlers: ask, judge and revise (the quality loop)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from law import calc
from law.handlers.base import Runtime
from law.llm import TruncatedError, chat_with_tools, image_part
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

PASS_SCORE = 4

RUBRIC = """Score the RESPONSE to the REQUEST from 1 to 5.
5 = correct, complete, well structured, ready for a demanding client
4 = correct and useful, cosmetic issues only
3 = mostly right with one material gap or error
2 = significant errors or omissions
1 = wrong or unusable
Check correctness, whether it answers the request, structure, clarity and invented facts.
Recompute every number, rate and total with the run_python tool before scoring; a wrong figure caps the score at 2.
Reply with JSON only: {"score": <1-5>, "issues": ["<problem and fix>"], "summary": "<one sentence>"}"""


def parse_verdict(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {"score": 0, "issues": ["judge returned no JSON"], "summary": text[:200]}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"score": 0, "issues": ["judge returned invalid JSON"], "summary": text[:200]}
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


async def answer(rt: Runtime, role: str, messages: list[dict[str, Any]], ctx: Context | None = None) -> str:
    """Ask the role's model at the role's reasoning effort, with the calculator if the role has it.
    If the model thinks itself out of tokens, retry one step lower."""
    r = rt.roles.get(role)
    on_thought = ctx.thought if ctx is not None else None

    async def ask_at(effort: str) -> str:
        if "python" in r.tools:
            async def run_tool(name: str, args: dict[str, Any]) -> str:
                if name != "run_python":
                    return f"error: unknown tool {name}"
                return await calc.run_python(rt.paths.runtime / "calc" / "bin" / "python", rt.paths.tmp / "calc",
                                             str(args.get("code", "")))
            return await chat_with_tools(rt.llm, rt.model_id(role), messages, [calc.TOOL_SPEC], run_tool,
                                         temperature=r.temperature, reasoning=effort, on_thought=on_thought)
        return await rt.llm.chat(rt.model_id(role), messages, temperature=r.temperature, reasoning=effort,
                                 on_thought=on_thought)

    try:
        return await ask_at(r.reasoning)
    except TruncatedError:
        return await ask_at(LOWER[r.reasoning])


def make_handlers(rt: Runtime) -> dict[str, Any]:
    async def ask(ctx: Context) -> Outcome:
        p = ctx.job.payload
        role = p.get("role", "assistant")
        images = _images(p)
        if images and not rt.reg.model(rt.model_id(role)).vision:
            raise Fatal(f"the {role} role's model cannot see images; use assistant, analyst or designer")
        if p.get("quality", True) and ctx.job.parent is None:
            seen = {"images": images} if images else {}
            return Stages([
                {"kind": "draft", "payload": {"role": role, "prompt": p["prompt"], **seen}},
                {"kind": "judge", "payload": {"prompt": p["prompt"]}},
                {"kind": "revise", "payload": {"role": role, "prompt": p["prompt"], **seen}},
            ])
        await ctx.progress(f"{role} is answering")
        text = await answer(rt, role, build_messages(rt, role, p["prompt"], p.get("history"), images), ctx)
        return Done({"answer": text})

    async def draft(ctx: Context) -> Outcome:
        return await ask(ctx)

    async def judge(ctx: Context) -> Outcome:
        carry = ctx.job.payload["_carry"]
        await ctx.progress("independent review")
        text = await answer(rt, "reviewer", build_messages(
            rt, "reviewer", f"{RUBRIC}\n\nREQUEST:\n{ctx.job.payload['prompt']}\n\nRESPONSE:\n{carry['answer']}"), ctx)
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
        text = await answer(rt, role, build_messages(rt, role, prompt, images=_images(ctx.job.payload)), ctx)
        return Done({"answer": text, "verdict": verdict, "revised": True})

    return {"ask": ask, "draft": draft, "judge": judge, "revise": revise}
