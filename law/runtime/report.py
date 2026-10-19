"""One job as a Markdown report — the prompt, the spec, every step (thinking, tool calls,
delegations), the final answer, sub-jobs and the undo command. Used by `law job <id> --md`
and the dashboard's "⤓ .md" button."""
from __future__ import annotations

import json
import time

from .db import Queue


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def job_markdown(q: Queue, jid: int, store=None, project=None) -> str:
    job = q.get(jid)
    if not job:
        raise KeyError(f"no job {jid}")
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    prompt = payload.get("task") or payload.get("prompt") or payload.get("text") or \
        ((payload.get("messages") or [{}])[-1].get("content") if payload.get("messages") else "")
    secs = (job.get("finished") or time.time()) - (job.get("started") or job.get("created") or time.time())
    out = [f"# Job {jid} — {job.get('kind')} · @{job.get('persona') or job.get('model')}", "",
           "| | |", "|---|---|",
           f"| status | **{job.get('status')}** |", f"| model | `{job.get('model')}` |",
           f"| source | {job.get('source')} |", f"| created | {_when(job.get('created'))} |",
           f"| finished | {_when(job.get('finished'))} |", f"| duration | {secs / 60:.1f} min |"]
    if payload.get("cwd"):
        out.append(f"| project | `{payload['cwd']}` |")
    if job.get("parent_id"):
        out.append(f"| parent job | {job['parent_id']} |")
    if prompt:
        out += ["", "## Request", "", str(prompt).strip()]
    if store is not None and project is not None:
        spec = store.get_spec(project, jid)
        if spec:
            out += ["", "## Task spec", "", spec.strip()]
    steps = []
    snapshot = None
    for e in q.events(jid):
        d, t = e["data"] or {}, e["type"]
        if t == "snapshot":
            snapshot = d
        if t != "agent":
            continue
        tag = f"**[{d.get('step')}]**" if d.get("step") else ""
        if d.get("think"):
            steps.append(f"- {tag} 🧠 _{' '.join(str(d['think']).split())[:400]}_")
        elif d.get("say"):
            steps.append(f"- {tag} 💬 {' '.join(str(d['say']).split())[:600]}")
        elif d.get("note"):
            steps.append(f"- {tag} · {d['note']}")
        elif d.get("tool") == "delegate" and not d.get("output"):
            steps.append(f"- {tag} → delegated to @{d.get('persona')} (job {d.get('child')})")
        elif d.get("running"):
            steps.append(f"- {tag} 🔧 `{d.get('tool')}` `{str(d.get('args', ''))[:300]}`")
        elif d.get("tool"):
            body = str(d.get("output") or "").strip()
            steps.append(f"  - ↳ ({d.get('seconds', '?')}s)\n\n    ```\n    "
                         + body[:400].replace("\n", "\n    ") + "\n    ```")
    if steps:
        out += ["", "## Steps", ""] + steps
    if job.get("status") == "done":
        try:
            res = json.loads(job.get("result") or "{}")
        except ValueError:
            res = {}
        answer = (res.get("message") or {}).get("content") or ""
        if answer:
            out += ["", "## Result", "", answer.strip()]
    if job.get("error"):
        out += ["", "## Error", "", "```", str(job["error"]).strip(), "```"]
    kids = q.children(jid)
    if kids:
        out += ["", "## Sub-jobs", ""] + [f"- job {k['id']} · @{k.get('persona')} · {k['status']}" for k in kids]
    if snapshot and payload.get("cwd"):
        out += ["", "## Undo", "", f"`law rollback {payload['cwd']} --to {snapshot.get('id')}`"]
    return "\n".join(out) + "\n"
