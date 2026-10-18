"""`law agent` — give an agent a task in a project folder.

    law agent "build tic tac toe 3x3 for the web, run it on local docker" -C ~/MLX-AI/source/ttt
    law agent -p coder "add tests for utils.py" -C .
    law agent --resume 42           continue a failed / cancelled / stopped agent job
    law agent --show-memory [-C .]  show the project's instructions and memory
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .paths import get_paths
from .project import ensure_project
from .registry import load
from .runtime.db import Queue
from .runtime.store import Store
from .util import err, ok, warn


def _dim(s: str) -> str:
    return f"\033[90m{s}\033[0m" if sys.stdout.isatty() else s


def _follow(q: Queue, jid: int) -> int:
    from .runcmds import tail_job, _worker_warning
    _worker_warning(q)
    try:
        st = tail_job(q, jid)
    except KeyboardInterrupt:
        q.cancel(jid, reason="Ctrl+C")
        warn(f"cancelled job {jid} (and its sub-jobs). Continue it later with: law agent --resume {jid}")
        return 130
    job = q.get(jid) or {}
    if st == "done":
        try:
            res = json.loads(job.get("result") or "{}")
        except json.JSONDecodeError:
            res = {}
        final = (res.get("message") or {}).get("content") or ""
        if final.strip():
            print("\n" + final.strip())
        ag = res.get("agent") or {}
        print(_dim(f"\n[{ag.get('steps')} step(s) · {ag.get('tools_used', 0)} tool call(s) · "
                   f"project {ag.get('project')}]"))
        if ag.get("undo"):
            print(_dim(ag["undo"]))
        return 0
    if st in ("failed", "cancelled"):
        print(_dim(f"continue it with: law agent --resume {jid}"))
    return 1


def show_memory(folder: Path) -> int:
    project = Path(folder).expanduser().resolve()
    store = Store(get_paths().agent_db)
    agent_md = project / ".local-agent" / "AGENT.md"
    print(f"── {agent_md}")
    if agent_md.exists():
        print(agent_md.read_text(errors="replace").rstrip())
    else:
        print("(none — write one with `law init-project` to give agents project instructions)")
    print()
    print("── project memory (law store)")
    md = store.memory_markdown(project)
    print(md if md else "(no memories saved yet)")
    print()
    n = store.spec_count(project)
    if n:
        print(f"── {n} task spec(s) saved for this project (latest: job {store.latest_spec_job(project)})")
    return 0


def cmd_agent(a) -> int:
    p = get_paths()
    q = Queue(p.queue_db)
    folder = Path(a.dir).expanduser().resolve()
    if a.show_memory:
        return show_memory(folder)
    if a.resume is not None:
        job = q.get(a.resume)
        if not job:
            err(f"no job {a.resume}"); return 1
        if job.get("kind") != "agent":
            err(f"job {a.resume} is a {job.get('kind')} job, not an agent job"); return 1
        if job["status"] in ("queued", "running", "waiting"):
            ok(f"job {a.resume} is still {job['status']} — following it")
        elif job["status"] == "done":
            err(f"job {a.resume} already finished — start a new task instead"); return 1
        elif not q.revive(a.resume):
            err(f"could not resume job {a.resume} (status {job['status']})"); return 1
        else:
            st = q.get_state(a.resume) or {}
            ok(f"resuming job {a.resume} from step {st.get('step', 0)}")
        return _follow(q, a.resume)

    task = " ".join(a.task) if a.task else ("" if sys.stdin.isatty() else sys.stdin.read())
    if not task.strip():
        err('give the agent a task, e.g.  law agent "add a README" -C ~/MLX-AI/source/myapp'); return 2
    reg = load(p)
    persona = a.persona or reg.raw.get("defaults", {}).get("persona", "general")
    if persona not in reg.personas:
        err(f"unknown persona '{persona}' — see `law models`"); return 2
    from .agent.runner import AgentRefused, agent_cfg, check_project
    try:
        project = check_project(folder, p.home)
    except AgentRefused as e:
        err(str(e)); return 2
    ensure_project(project)
    translate = agent_cfg(reg)["translate"] if a.translate is None else a.translate
    payload = {"task": task, "cwd": str(project), "depth": 0, "translate": translate,
               "messages": [{"role": "user", "content": task}]}
    if a.max_steps:
        payload["max_steps"] = a.max_steps
    model = reg.personas[persona]["model"]
    jid = q.enqueue(model, payload, persona=persona, source="cli", kind="agent")
    print(_dim(f"[agent job {jid} · @{persona} → {model} · project {project}]"))
    if a.detach:
        print(f"queued. Follow it: law agent --resume {jid}   ·   law jobs   ·   cancel: law cancel {jid}")
        return 0
    return _follow(q, jid)


def _project_of(q: Queue, job: dict) -> Path | None:
    try:
        cwd = json.loads(job.get("payload") or "{}").get("cwd")
        return Path(cwd).expanduser().resolve() if cwd else None
    except (ValueError, TypeError):
        return None


def cmd_job(a) -> int:
    """Show what an agent job saved to law's store: its spec, its raw model replies, the brief
    it was handed (delegated jobs). Replaces digging through <project>/.local-agent/ files."""
    paths = get_paths()
    q = Queue(paths.queue_db)
    store = Store(paths.agent_db)
    job = q.get(a.id)
    if not job:
        err(f"no job {a.id} in the queue")
        return 1
    project = _project_of(q, job)
    want_spec, want_replies, want_handoff = a.spec, a.replies, a.handoff
    if not (want_spec or want_replies or want_handoff):          # default: a summary + the spec
        prompt = ""
        try:
            p = json.loads(job.get("payload") or "{}")
            prompt = p.get("task") or (p.get("messages") or [{}])[-1].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        print(f"── job {a.id} · {job.get('status')} · {job.get('kind')} · @{job.get('persona') or job.get('model')}"
              f" · {job.get('source')}")
        if prompt:
            print(f"   {' '.join(str(prompt).split())[:200]}")
        if job.get("error"):
            print(f"   error: {job['error']}")
        if project:
            print(f"   project: {project}")
            print(f"   replies saved: {store.run_count(project, a.id)}")
        print(_dim("\n   add --spec, --replies or --handoff to see the saved detail"))
        want_spec = True
    if project is None:
        warn("this job has no project folder recorded — nothing stored.")
        return 0
    if want_spec:
        spec = store.get_spec(project, a.id)
        print("\n── task spec")
        print(spec.rstrip() if spec else "(no spec saved — the request ran as written)")
    if want_handoff:
        h = store.get_handoff(project, a.id)
        print("\n── delegation brief")
        print(h.rstrip() if h else "(not a delegated sub-job)")
    if want_replies:
        runs = store.get_runs(project, a.id)
        print(f"\n── {len(runs)} raw model repl{'y' if len(runs) == 1 else 'ies'}")
        for r in runs:
            print(_dim(f"\n   ┄┄ {r['step']} ┄┄"))
            body = r["content"]
            print(body if len(body) <= 4000 else body[:4000] + _dim(f"\n   …[{len(body) - 4000} more chars]"))
    return 0


def add_parsers(sub) -> None:
    s = sub.add_parser("job", help="show what an agent job saved (spec / replies / brief) from law's store")
    s.add_argument("id", type=int, help="the job id (see `law jobs`)")
    s.add_argument("--spec", action="store_true", help="show the task spec the translator produced")
    s.add_argument("--replies", action="store_true", help="show every raw model reply (for debugging)")
    s.add_argument("--handoff", action="store_true", help="show the brief a delegated sub-job was given")
    s.set_defaults(fn=cmd_job)

    s = sub.add_parser("agent", help='give an agent a task with tools:  law agent "…" -C <project folder>')
    s.add_argument("task", nargs="*", help="what to do, in plain words (or pipe it on stdin)")
    s.add_argument("-C", "--dir", default=".", help="project folder the agent works in (default: current folder)")
    s.add_argument("-p", "--persona", help="persona (default: general; orchestrator for big multi-part jobs)")
    s.add_argument("--translate", dest="translate", action="store_true", default=None,
                   help="turn the request into a task spec first (default: runtime.agent.translate)")
    s.add_argument("--no-translate", dest="translate", action="store_false",
                   help="give the request to the agent exactly as written")
    s.add_argument("--max-steps", type=int, help="model turns before it stops (default: runtime.agent.max_steps)")
    s.add_argument("-d", "--detach", action="store_true", help="queue it and return immediately")
    s.add_argument("--resume", type=int, metavar="JOB", help="continue a failed / cancelled agent job, or follow a running one")
    s.add_argument("--show-memory", action="store_true", help="show the project's AGENT.md and memory, then exit")
    s.set_defaults(fn=cmd_agent)
