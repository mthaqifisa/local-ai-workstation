"""Search and multi-step task handlers (OpenCode runs the coding and research agents)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from law.handlers.base import Runtime, Stalled, run_cmd
from law.handlers.core import PASS_SCORE, parse_verdict
from law.queue.worker import Context, Done, Fatal, Outcome, Stages

# Roles whose output is code or files get an independent review + one fix pass by default.
REVIEWED_ROLES = {"coder", "writer", "designer", "architect", "analyst"}

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
    label = TOOL_ICONS.get(tool, f"🔧 {tool}")
    if not target:
        return label
    lines = [ln.strip() for ln in str(target).replace(str(Path.home()), "~").splitlines() if ln.strip()]
    text = lines[0] if lines else ""
    return f"{label} {text[:140]}"


# An agent that changes no file for this long is stuck (seen: a 2h20m re-read loop across context compactions).
# It is stopped and its session resumed with a nudge to build and fix, at most STALL_NUDGES times.
STALL_MINUTES = 25
STALL_NUDGES = 3
NUDGE = ("You have not changed any file for {minutes} minutes. Stop reading and exploring. Run the project's "
         "build or tests now, fix the first error they report (open only the file it names), and repeat. "
         "Commit when a gate passes.")


def is_file_change(line: str) -> bool:
    summary = describe_opencode_line(line)
    return summary is not None and summary.startswith("✏️")


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


def agent_env(rt: Runtime, role: str) -> dict[str, str]:
    """Role config plus XDG dirs so OpenCode keeps its sessions, cache and state under ~/LAW, not ~/.local."""
    base = rt.paths.data / "opencode"
    cache = rt.paths.cache
    return {"OPENCODE_CONFIG": str(rt.paths.config / f"opencode-{role}.json"),
            "XDG_DATA_HOME": str(base / "data"), "XDG_STATE_HOME": str(base / "state"),
            "XDG_CACHE_HOME": str(cache / "opencode"),
            # build-tool caches the agents fill (measured: ~/.m2 and ~/.npm grew during one Java/Angular build)
            "npm_config_cache": str(cache / "npm"), "MAVEN_OPTS": f"-Dmaven.repo.local={cache / 'm2'}",
            "GRADLE_USER_HOME": str(cache / "gradle"), "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv")}


def make_handlers(rt: Runtime) -> dict[str, Any]:
    async def search(ctx: Context) -> Outcome:
        hits = await searx(rt, ctx.job.payload["query"], int(ctx.job.payload.get("limit", 8)))
        return Done({"results": hits})

    async def _agent(ctx: Context, role: str, prompt: str, workdir: Path, *, writes: bool = True) -> str:
        """Run one OpenCode agent. A writing agent that stalls (no file change for STALL_MINUTES) is stopped and
        its session resumed with a nudge to build and fix."""
        rt.roles.get(role)  # validate
        model = f"law/{rt.model_id(role)}"
        stall_min = float(ctx.job.payload.get("stall_min", STALL_MINUTES))
        await ctx.progress(f"{role} agent working in {workdir}")
        base = ["opencode", "run", "--agent", role, "-m", model, "--format", "json", "--dir", str(workdir)]
        cmd = [*base, prompt]
        for attempt in range(STALL_NUDGES + 1):
            try:
                out = await run_cmd(ctx, cmd, cwd=workdir, env=agent_env(rt, role), describe=describe_opencode_line,
                                    timeout_s=float(ctx.job.payload.get("timeout_s", 4 * 3600)),
                                    stall_s=stall_min * 60 if writes and stall_min > 0 else None,
                                    is_work=is_file_change)
                return parse_opencode_events(out)
            except Stalled as exc:
                sid = session_id(exc.output)
                if sid is None or attempt == STALL_NUDGES:
                    raise Fatal(f"{role} agent stalled: {exc}") from exc
                await ctx.progress(f"⚠️ no file changed for {stall_min:.0f} min: nudging the agent to build and fix")
                cmd = [*base, "--session", sid, NUDGE.format(minutes=f"{stall_min:.0f}")]
        raise AssertionError("unreachable")

    def _scoped(p: dict[str, Any]) -> str:
        """When the user named a directory, the agent must build there, not in a new folder."""
        if p.get("dir"):
            return (f"Work in this directory and create the project files directly in it: {p['dir']}\n"
                    f"Do not create a different project folder.\n\n{p['prompt']}")
        return str(p["prompt"])

    def _workdir(p: dict[str, Any]) -> Path:
        workdir = Path(p.get("dir") or rt.paths.projects).expanduser()
        if not workdir.is_dir():
            raise Fatal(f"directory does not exist: {workdir}")
        return workdir

    async def task(ctx: Context) -> Outcome:
        """Run a role as an OpenCode agent. Code-writing roles get an independent review and a fix pass."""
        p = ctx.job.payload
        role = p.get("role", "coder")
        workdir = _workdir(p)
        if p.get("review", role in REVIEWED_ROLES) and ctx.job.parent is None:
            base = {"prompt": p["prompt"], "dir": str(workdir), "role": role}
            return Stages([{"kind": "task_work", "payload": base}, {"kind": "task_review", "payload": base},
                           {"kind": "task_fix", "payload": base}])
        answer = await _agent(ctx, role, _scoped(p), workdir, writes=role in REVIEWED_ROLES)
        return Done({"answer": answer, "dir": str(workdir)})

    async def task_work(ctx: Context) -> Outcome:
        p = ctx.job.payload
        return Done({"answer": await _agent(ctx, p["role"], _scoped(p), _workdir(p)), "dir": p["dir"]})

    async def task_review(ctx: Context) -> Outcome:
        p = ctx.job.payload
        report = ctx.job.payload["_carry"].get("answer", "")
        prompt = (f"{CODE_RUBRIC}\n\nThe REQUEST was:\n{p['prompt']}\n\nThe author reported:\n{report[:4000]}\n\n"
                  "Inspect the files the author created or changed (the project may be in a new folder under "
                  "~/SourceCode named after it). Verify claims against the code; do not trust the report.")
        verdict = parse_verdict(await _agent(ctx, "reviewer", prompt, _workdir(p), writes=False))
        return Done({"review": verdict})

    async def task_fix(ctx: Context) -> Outcome:
        p = ctx.job.payload
        carry = ctx.job.payload["_carry"]
        verdict = carry["review"]
        if verdict["score"] >= PASS_SCORE:
            return Done({"answer": carry["answer"], "review": verdict, "fixed": False, "dir": p["dir"]})
        issues = "\n".join(f"- {i}" for i in verdict["issues"]) or "- raise the quality to client-ready"
        prompt = (f"An independent reviewer scored your work {verdict['score']}/5 for this request:\n{p['prompt']}\n\n"
                  f"Fix every issue below, then rerun the project's checks until they pass and report the output:\n"
                  f"{issues}")
        answer = await _agent(ctx, p["role"], prompt, _workdir(p))
        return Done({"answer": answer, "review": verdict, "fixed": True, "dir": p["dir"]})

    return {"search": search, "task": task, "task_work": task_work, "task_review": task_review, "task_fix": task_fix}
