"""Agent jobs (kind "agent") — run inside the single worker, one at a time.

First start of a job:
  1. check the project folder (refuses / and your home folder itself) and create it if missing
  2. SNAPSHOT the project (undo point — `law rollback <folder>` restores it)
  3. top-level jobs only: the request translator turns the casual request into a task spec
     (saved to law's central store, keyed by project + job id); sub-jobs get a precise brief
  4. system prompt = persona role + rules + optional AGENT.md + saved project memory

Then the loop, one model turn per step (max_steps):
  model answers → tool calls are run → results go back to the model → … until the model
  answers without calling a tool (= its final report).

Making local models reliable (learned from a real M5 Pro run, where qwen38-27b "finished"
after one 80-token reply without creating anything):
  - turns are NOT streamed (agent.stream: false): the model server parses tool calls more
    reliably for complete replies
  - thinking that leaks into the answer ("…</think>") is moved out of it
  - tool calls the model wrote as TEXT (<tool_call>…, <function=…>, JSON) are recovered
  - if the model "finishes" without ever calling a tool — so nothing happened on the Mac —
    or with an empty reply, it is told so and asked to act (agent.nudges times); if it
    still doesn't, the report says plainly that nothing was changed
  - every raw reply is saved to law's store (runs, keyed by project + job id) for debugging

The whole conversation is saved in the job (jobs.state) after every step, so a job that is
retried, interrupted by a restart, or resumed with `law agent --resume <id>` continues
from the last finished step instead of starting over.

Delegation: the `delegate` tool creates a sub-job (kind agent, another persona, same
project, priority above the parent, depth + 1). The parent is parked as 'waiting'; the
single worker runs the sub-job next (swapping models if needed); when every sub-job has
ended the parent is queued again and receives each sub-job's final report (or its error)
as the tool result. Briefs are saved to law's store (handoffs). Delegation depth is limited
(max_depth) so agents can't hand work around forever.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..project import ensure_project
from ..runtime.modelproc import ModelError, PromptTooSlow
from ..runtime.store import Store
from ..undo import Undo, UndoError
from . import prompts
from .textcalls import parse_text_calls, split_think, strip_calls
from .tools import DELEGATE_SCHEMA, SCHEMAS, ToolCtx, cap, execute

DEFAULTS = {"max_steps": 80, "max_depth": 2, "translate": True, "shell_timeout_s": 3600,
            "tool_output_chars": 8000, "max_tokens": 8192, "context_chars": 48000,
            "nudges": 2, "save_replies": True, "stream": True,
            "keep_jobs": 200, "keep_days": 30, "auto_resume": 3}


class WaitingForChildren(Exception):
    def __init__(self, children: list[int]):
        super().__init__(f"waiting for sub-jobs {children}")
        self.children = children


class ContinueJob(Exception):
    """The agent reached its step budget but is still making progress (using tools), so the job
    is put back on the queue to continue from its saved state with an extended budget — instead
    of silently stopping and being reported as 'done'. Bounded by runtime.agent.auto_resume."""


class AgentRefused(ModelError):
    retry = False


def agent_cfg(reg, payload: dict | None = None) -> dict:
    cfg = {**DEFAULTS, **((reg.raw.get("runtime") or {}).get("agent") or {})}
    for k in ("max_steps", "translate", "auto_resume"):
        if payload and k in payload and payload[k] is not None:
            cfg[k] = payload[k]
    return cfg


def check_project(folder: str | Path, home: Path) -> Path:
    p = Path(folder).expanduser().resolve()
    home = Path(home).expanduser().resolve()
    if p == Path(p.anchor) or p == home or p in home.parents:
        raise AgentRefused(f"refusing to use {p} as the project folder: an agent works inside ONE project "
                           f"folder (its files are snapshotted for undo). Use e.g. -C ~/MLX-AI/source/<name>.")
    return p


def _short(args) -> str:
    s = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
    return s if len(s) <= 300 else s[:300] + "…"


def _history_calls(calls: list[dict]) -> list[dict]:
    """Copies of the tool calls for the saved conversation, with each function's `arguments`
    as a JSON STRING — the OpenAI wire format the model server's request schema requires (a
    dict is rejected with HTTP 422). Chat templates that iterate arguments with `|items`
    (Qwen3, Qwen3-Coder, …) are served by the server's own message converter, which turns the
    string back into a mapping; law just has to send a valid, consistent string so neither the
    schema nor the template trips. Normalising here also guards against a model that emits a
    dict natively."""
    out = []
    for tc in calls:
        fn = dict(tc.get("function") or {})
        raw = fn.get("arguments")
        if isinstance(raw, str):
            fn["arguments"] = raw if raw.strip() else "{}"
        elif raw is None:
            fn["arguments"] = "{}"
        else:
            try:
                fn["arguments"] = json.dumps(raw)
            except (TypeError, ValueError):
                fn["arguments"] = "{}"
        out.append({**tc, "function": fn})
    return out


def _total_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def compact(messages: list[dict], limit: int) -> None:
    """Keep the conversation within `limit` characters by shortening OLD tool results."""
    total = _total_chars(messages)
    if total <= limit:
        return
    for m in messages[2:-6]:                     # never the system prompt, task, or the latest turns
        if m.get("role") == "tool" and len(m.get("content") or "") > 800:
            c = m["content"]
            total -= len(c)
            m["content"] = c[:400] + f"\n…[older output shortened, {len(c):,} characters]…\n" + c[-300:]
            total += len(m["content"])
            if total <= limit:
                return


def shrink_hard(messages: list[dict], limit: int) -> int:
    """Last-resort trim when even a compacted prompt was too slow to read: shorten EVERY tool
    result (keep only the most recent one full) and over-long assistant notes, oldest first,
    until the conversation fits `limit`. Returns the characters removed. The system prompt and
    the user task (messages[0], messages[1]) are never touched."""
    before = _total_chars(messages)
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    keep_full = set(tool_idx[-1:])               # the latest tool result stays intact
    for i, m in enumerate(messages):
        if _total_chars(messages) <= limit:
            break
        if i < 2 or i in keep_full:
            continue
        c = str(m.get("content") or "")
        if m.get("role") == "tool" and len(c) > 300:
            m["content"] = c[:200] + f"\n…[trimmed {len(c):,} chars to fit context]…\n" + c[-150:]
        elif m.get("role") == "assistant" and len(c) > 1200:
            m["content"] = c[:600] + f"\n…[trimmed {len(c):,} chars]…\n" + c[-300:]
    return before - _total_chars(messages)


def child_report(q, child_id: int) -> str:
    job = q.get(child_id) or {}
    st = job.get("status")
    if st == "done":
        try:
            content = json.loads(job.get("result") or "{}").get("message", {}).get("content", "")
        except json.JSONDecodeError:
            content = ""
        return f"Sub-job {child_id} ({job.get('persona')}) finished. Its report:\n{content or '(empty report)'}"
    if st == "failed":
        return f"SUB-TASK FAILED: sub-job {child_id} ({job.get('persona')}) failed: {job.get('error')}"
    return f"SUB-TASK CANCELLED: sub-job {child_id} ({job.get('persona')}) was cancelled ({job.get('error') or ''})"


def _turn(w, jid: int, base: str, messages: list, params: dict, cfg: dict) -> dict:
    if cfg.get("stream"):
        r = w.complete(jid, base, messages, params)
        return {"result": r["result"], "raw": r["result"]}
    return w.complete_once(jid, base, messages, params)


def _save_reply(store: Store, project: Path, jid: int, step, raw, cfg: dict) -> None:
    if not cfg.get("save_replies"):
        return
    try:
        label = f"step-{step}" if isinstance(step, int) else str(step)
        store.put_run(project, jid, label, raw)
    except (TypeError, ValueError, sqlite3.Error):
        pass


def _translate(w, jid: int, base: str, task: str, project: Path, cfg: dict, store: Store) -> dict | None:
    msgs = [{"role": "system", "content": prompts.TRANSLATOR_SYSTEM},
            {"role": "user", "content": f"ENVIRONMENT:\n{prompts.environment(project, w.reg.ports)}\n\n"
                                        f"REQUEST:\n{task}"}]
    params = {"temperature": 0.1, "max_tokens": 6000}
    for attempt in range(2):
        try:
            r = _turn(w, jid, base, msgs, params, cfg)
        except ModelError as e:
            w.q.add_event(jid, "warning", {"message": f"request translator failed ({e}) — using the request as-is"})
            return None
        _save_reply(store, project, jid, f"translate-{attempt + 1}", r["raw"], cfg)
        m = r["result"]["message"]
        spec = prompts.parse_spec(m.get("content") or "") or prompts.parse_spec(m.get("reasoning_content") or "")
        if spec:
            return spec
        msgs += [{"role": "assistant", "content": (m.get("content") or "")[:2000]},
                 {"role": "user", "content": prompts.TRANSLATOR_RETRY}]
    w.q.add_event(jid, "warning", {"message": "request translator gave no usable spec — using the request as-is"})
    return None


def _start(w, job: dict, payload: dict, project: Path, cfg: dict, can_delegate: bool, store: Store) -> dict:
    jid = job["id"]
    task = payload.get("task") or ""
    snap = None
    try:
        if payload.get("plan_only"):
            raise UndoError("plan only — nothing will be changed")
        sid, n, skipped = Undo(project).snapshot(f"before agent job {jid}: {' '.join(task.split())[:60]}")
        snap = sid
        w.q.add_event(jid, "snapshot", {"id": sid, "project": str(project), "changed": n, "skipped": skipped})
    except (UndoError, OSError) as e:
        if not payload.get("plan_only"):
            w.q.add_event(jid, "warning", {"message": f"could not take an undo snapshot ({e}) — "
                                                      "continuing without one"})
    first = task
    if payload.get("spec_text"):                 # a spec approved earlier (Telegram /plan → /go)
        first = str(payload["spec_text"])
        title = first.splitlines()[0].lstrip("# ").replace("Task: ", "") if first.strip() else ""
        store.put_spec(project, jid, first, title)
        w.q.add_event(jid, "spec", {"title": title, "assignee": job.get("persona"), "assumptions": [],
                                    "questions": [], "job": jid})
    elif (cfg["translate"] or payload.get("plan_only")) and payload.get("depth", 0) == 0:
        base = w.mm.ensure(job["model"])
        w.check(jid)
        spec = _translate(w, jid, base, task, project, cfg, store)
        if spec:
            first = prompts.spec_to_prompt(spec, task, persona=job.get("persona"))
            store.put_spec(project, jid, first, spec.get("title"))
            w.q.add_event(jid, "spec", {"title": spec.get("title"), "assignee": spec.get("assignee"),
                                        "assumptions": spec.get("assumptions"),
                                        "questions": spec.get("blocking_questions"), "job": jid})
    persona = job.get("persona") or "general"
    sysmsg = prompts.system_prompt(persona, project, can_delegate=can_delegate,
                                   personas=list(w.reg.personas), store=store)
    return {"messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": first}],
            "step": 0, "snapshot": snap, "pending": {}, "project": str(project),
            "tools_used": 0, "nudges": 0, "spec": first if first != task else None}


def _delegate(w, job: dict, payload: dict, project: Path, args: dict, store: Store) -> tuple[int | None, str]:
    persona = str(args.get("persona") or "").strip()
    task = str(args.get("task") or "").strip()
    if persona not in w.reg.personas:
        return None, f"ERROR: unknown persona '{persona}'. Available: {', '.join(w.reg.personas)}"
    if not task:
        return None, "ERROR: delegate needs a task"
    model = w.reg.personas[persona]["model"]
    child = w.q.enqueue(model, {"task": task, "cwd": str(project), "depth": payload.get("depth", 0) + 1,
                                "translate": False, "messages": [{"role": "user", "content": task}]},
                        persona=persona, source="agent", parent_id=job["id"],
                        priority=int(job.get("priority") or 0) + 1, kind="agent")
    store.put_handoff(project, child,
                      f"# Brief for @{persona} (sub-job {child} of job {job['id']})\n\n{task}\n",
                      parent_id=job["id"], persona=persona)
    return child, f"delegated to {persona} as sub-job {child}"


def normalize(msg: dict, names: set[str]) -> tuple[str, str, list[dict], bool]:
    """→ (answer text, reasoning, tool calls, rescued?) from a raw assistant message."""
    content, reasoning = split_think(msg.get("content") or "", msg.get("reasoning_content") or "")
    calls = [c for c in (msg.get("tool_calls") or []) if (c.get("function") or {}).get("name")]
    rescued = False
    if not calls:
        calls = parse_text_calls(content, names)
        if calls:
            content, rescued = strip_calls(content), True
        else:
            calls = parse_text_calls(reasoning, names)
            rescued = bool(calls)
    return content, reasoning, calls, rescued


def run_agent(w, job: dict) -> dict:
    jid = job["id"]
    payload = json.loads(job["payload"])
    cfg = agent_cfg(w.reg, payload)
    project = check_project(payload.get("cwd") or ".", w.paths.home)
    ensure_project(project)
    store = Store(w.paths.agent_db)
    depth = int(payload.get("depth", 0))
    can_delegate = depth < int(cfg["max_depth"])
    tools = SCHEMAS + ([DELEGATE_SCHEMA] if can_delegate else [])
    names = {t["function"]["name"] for t in tools}
    tctx = ToolCtx(project=project, cfg=cfg, touch=w._touch, check=lambda: w.check(jid),
                   searxng_port=int(w.reg.ports.get("searxng", 8888)), store=store,
                   telegram_chat_id=payload.get("telegram_chat_id"), paths=w.paths)

    state = w.q.get_state(jid)
    if state is None:
        state = _start(w, job, payload, project, cfg, can_delegate, store)
        w.q.save_state(jid, state)
    elif state.get("step"):
        w.q.add_event(jid, "agent", {"step": state["step"], "note": "continuing from the saved conversation"})
    # normalise tool-call arguments in the saved conversation to JSON strings, so a job saved
    # before this fix (or one carrying dict arguments) is schema-valid when resumed.
    for m in state["messages"]:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            m["tool_calls"] = _history_calls(m["tool_calls"])
    if payload.get("plan_only"):                 # Telegram /plan: show the spec, run nothing
        spec = state.get("spec")
        return {"message": {"role": "assistant", "content": spec or "(the translator gave no usable spec)"},
                "finish_reason": "stop", "plan": {"task": payload.get("task") or "", "spec": spec}}
    state.setdefault("tools_used", sum(1 for m in state["messages"] if m.get("role") == "tool"))
    state.setdefault("nudges", 0)

    if state.get("pending"):                     # sub-jobs ended → feed their reports back
        for tcid, child in list(state["pending"].items()):
            report = child_report(w.q, int(child))
            state["messages"].append({"role": "tool", "tool_call_id": tcid, "content": cap(report, tctx.limit)})
            w.q.add_event(jid, "agent", {"step": state["step"], "tool": "delegate", "child": child,
                                         "output": report[:400]})
        state["pending"] = {}
        w.q.save_state(jid, state)

    base_max = int(cfg["max_steps"])
    resumes = int(state.get("resumes", 0))
    max_steps = base_max * (resumes + 1)             # each auto-continue extends the ceiling
    run_start_tools = int(state.get("tools_used", 0))
    final = None
    while state["step"] < max_steps:
        w.check(jid)
        base = w.mm.ensure(job["model"])
        w.check(jid)
        state["step"] += 1
        step = state["step"]
        compact(state["messages"], int(cfg["context_chars"]))
        params = {"tools": tools, "tool_choice": "auto", "temperature": 0.2,
                  "max_tokens": int(cfg["max_tokens"])}
        try:
            r = _turn(w, jid, base, state["messages"], params, cfg)
        except PromptTooSlow as e:
            # the conversation grew until the model server gave up reading the prompt (its 300 s
            # limit). Don't fail the whole job: trim hard and try this turn once more. This is the
            # correctness-first recovery for long multi-step tasks (what broke long Telegram jobs).
            removed = shrink_hard(state["messages"], int(cfg["context_chars"]) // 2)
            w.q.add_event(jid, "warning", {"message": f"the prompt got too large to read in time — "
                                           f"trimmed {removed:,} characters of earlier output and retrying"})
            w.q.save_state(jid, state)
            if removed <= 0:
                raise
            r = _turn(w, jid, base, state["messages"], params, cfg)
        _save_reply(store, project, jid, step, r["raw"], cfg)
        content, reasoning, calls, rescued = normalize(r["result"]["message"], names)
        if reasoning.strip():
            w.q.add_event(jid, "agent", {"step": step, "think": reasoning[:4000]})
        if rescued:
            w.q.add_event(jid, "agent", {"step": step, "note": f"recovered {len(calls)} tool call(s) the model "
                                                                "wrote as text"})
        am: dict = {"role": "assistant", "content": content}
        if calls:
            for i, tc in enumerate(calls):
                tc["id"] = tc.get("id") or f"call_{jid}_{step}_{i}"
                tc["type"] = "function"
            am["tool_calls"] = _history_calls(calls)     # arguments as a JSON string (schema-valid)
        state["messages"].append(am)

        # repetition guard: a model stuck repeating the SAME step (same content + same tool calls) would
        # otherwise loop to the step ceiling and flood the chat (seen on a hard 'verify the Sent folder'
        # sub-task). Nudge it once to stop and summarise; if it keeps looping, end the job gracefully.
        sig = json.dumps([content[:300].strip(),
                          [(c.get("function") or {}).get("name") for c in calls],
                          [str((c.get("function") or {}).get("arguments"))[:60] for c in calls]])
        sigs = state.setdefault("recent_sigs", [])
        sigs.append(sig)
        del sigs[:-3]
        if len(sigs) == 3 and len(set(sigs)) == 1:
            state["recent_sigs"] = []
            if state.get("loop_nudges", 0) < 1:
                state["loop_nudges"] = state.get("loop_nudges", 0) + 1
                state["messages"].append({"role": "user", "content": prompts.LOOP_STOP})
                w.q.add_event(jid, "agent", {"step": step, "note": "it was repeating the same step — "
                                             "asked it to stop and give its final answer"})
                w.q.save_state(jid, state)
                continue
            w.q.add_event(jid, "warning", {"message": "repetition loop persisted — stopping the job"})
            final = content or ("I got stuck repeating the same step and stopped. The main action may "
                                "have completed — please verify.")
            w.q.save_state(jid, state)
            break

        if not calls:
            nudge = None
            if state["tools_used"] == 0:
                nudge = prompts.NUDGE
            elif not content.strip():
                nudge = prompts.EMPTY_NUDGE
            # an empty reply may be nudged `nudges` times; a real answer without tools only once —
            # it may simply be the right answer to a question that needs no action
            limit = int(cfg["nudges"]) if not content.strip() else min(1, int(cfg["nudges"]))
            if nudge and state["nudges"] < limit:
                state["nudges"] += 1
                if content:
                    w.q.add_event(jid, "agent", {"step": step, "say": content[:1500]})
                state["messages"].append({"role": "user", "content": nudge})
                w.q.add_event(jid, "agent", {"step": step, "note":
                                             "the model answered without using any tool — asking it to "
                                             "actually do the work" if nudge is prompts.NUDGE else
                                             "the model gave an empty reply — asking it to continue"})
                w.q.save_state(jid, state)
                continue
            final = content
            w.q.save_state(jid, state)
            break

        if content:
            w.q.add_event(jid, "agent", {"step": step, "say": content[:1500]})
        state["tools_used"] += len(calls)
        children: list[int] = []
        for tc in calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments")
            try:
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                args = None
            if args is None or not isinstance(args, dict):
                out = f"ERROR: the arguments for {name} were not valid JSON: {str(raw_args or '')[:200]}"
            elif name == "delegate" and can_delegate:
                child, out = _delegate(w, job, payload, project, args, store)
                if child is not None:
                    state["pending"][tc["id"]] = child
                    children.append(child)
                    w.q.add_event(jid, "agent", {"step": step, "tool": "delegate", "args": _short(args),
                                                 "child": child, "persona": args.get("persona")})
                    continue
            else:
                w.q.add_event(jid, "agent", {"step": step, "tool": name, "args": _short(args), "running": True})
                t0 = time.time()
                out = execute(name, args, tctx)
                w.q.add_event(jid, "agent", {"step": step, "tool": name, "seconds": round(time.time() - t0, 1),
                                             "output": out[:400]})
            state["messages"].append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        w.q.save_state(jid, state)
        if children:
            raise WaitingForChildren(children)

    if final is None:
        # hit the step ceiling without the model declaring it finished. If it's still making
        # progress (used tools this run) and we have auto-resumes left, continue the job itself
        # instead of stopping — this is what lets agents finish long builds on their own.
        auto_max = int(cfg.get("auto_resume", 3))
        progressed = state["tools_used"] > run_start_tools
        if progressed and resumes < auto_max:
            state["resumes"] = resumes + 1
            w.q.save_state(jid, state)
            w.q.add_event(jid, "agent", {"step": state["step"], "note":
                          f"reached the {max_steps}-step budget but still working — "
                          f"auto-continuing ({resumes + 1}/{auto_max})"})
            raise ContinueJob()
        last = next((m.get("content") for m in reversed(state["messages"])
                     if m.get("role") == "assistant" and m.get("content")), "")
        hit = f"{max_steps} steps" + (f" after {resumes} auto-continuations" if resumes else "")
        final = (f"Stopped: reached the step budget ({hit}) before finishing. The work so far is saved; "
                 f"re-run the same task to continue it." + (f"\n\nLast message:\n{last}" if last else ""))
    if state["tools_used"] == 0:
        final = ((final or "").strip() + "\n\n[law] Note: this agent did not use any tool, so nothing was "
                 f"changed on the Mac. The model's raw replies were saved to law's agent store "
                 f"(job {jid}).").strip()
    try:                                             # keep the debug store bounded (retention)
        store.prune(int(cfg.get("keep_jobs", 200)), float(cfg.get("keep_days", 30)))
    except (sqlite3.Error, TypeError, ValueError):
        pass
    hint = (f"\n\n[undo: law rollback {project} --to {state['snapshot']}]" if state.get("snapshot") else "")
    return {"message": {"role": "assistant", "content": final}, "finish_reason": "stop",
            "agent": {"steps": state["step"], "snapshot": state.get("snapshot"), "project": str(project),
                      "tools_used": state["tools_used"], "undo": hint.strip()}}
