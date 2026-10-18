"""CLI commands for the runtime and day-to-day use:
start / stop / restart / run / jobs / cancel / logs / pull / probe / webui-reset-password /
add-model / remove-model / snapshot / snapshots / rollback."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .doctor import cached
from .manifest import load_manifest
from .paths import get_paths
from .registry import load
from .runtime.db import TERMINAL, Queue
from .runtime.modelproc import kill_from_pidfile
from .steps.services import SERVICES, label, wanted
from .system import System
from .util import err, is_macos, ok, read_env, warn

_TTY = sys.stdout.isatty()


def _dim(s: str) -> str:
    return f"\033[90m{s}\033[0m" if _TTY else s


def _cyan(s: str) -> str:
    return f"\033[36m{s}\033[0m" if _TTY else s


def _agent_line(d: dict) -> str:
    step = f"[step {d.get('step')}]" if d.get("step") is not None else "[agent]"
    if d.get("say"):
        return f"{step} {d['say']}"
    if d.get("note"):
        return _dim(f"{step} {d['note']}")
    if d.get("tool") == "delegate":
        if d.get("output"):
            return _cyan(f"{step} ← sub-job {d.get('child')} ended: {d['output'].splitlines()[0][:160]}")
        return _cyan(f"{step} → delegated to @{d.get('persona')} as sub-job {d.get('child')}")
    if d.get("running"):
        return _cyan(f"{step} {d.get('tool')} {d.get('args', '')}")
    first = (d.get("output") or "").strip().splitlines()
    return _cyan(f"{step}   ↳ {d.get('seconds')}s: {(first[0] if first else '')[:160]}")


def _svc_ctx():
    from .engine import Ctx
    return Ctx(paths=get_paths(), sys=System())


# ── services ────────────────────────────────────────────────────────────
def cmd_start(a) -> int:
    if not is_macos():
        err("macOS only"); return 1
    ctx = _svc_ctx()
    for lab, (f, _body) in wanted(ctx).items():
        if not f.exists():
            err(f"{f.name} missing — run ./install.sh first"); return 1
        if ctx.sys.launchd_loaded(lab):
            ok(f"{lab} already running")
        else:
            ctx.sys.launchd_load(lab, f); ok(f"started {lab}")
    return 0


def cmd_stop(a) -> int:
    if not is_macos():
        err("macOS only"); return 1
    s, p = System(), get_paths()
    for n in ["watchdog", "telegram", *[x for x in reversed(list(SERVICES)) if x != "watchdog"]]:
        # watchdog first, so it doesn't restart the others
        if n == "telegram" and not s.launchd_loaded(label(n)):
            continue
        s.launchd_unload(label(n)); ok(f"stopped {label(n)}")
    if kill_from_pidfile(p.run / "model.pid"):
        ok("model unloaded")
    print("  Jobs stay in the queue and continue after `law start`.")
    return 0


def cmd_restart(a) -> int:
    if not is_macos():
        err("macOS only"); return 1
    ctx = _svc_ctx()
    s = ctx.sys
    for lab, (f, _body) in wanted(ctx).items():
        if not f.exists():
            err(f"{f.name} missing — run ./install.sh first"); return 1
        if s.launchd_loaded(lab):
            s.launchd_kickstart(lab); ok(f"restarted {lab}")
        else:
            s.launchd_load(lab, f); ok(f"started {lab}")     # kickstart can't revive an unloaded job
    return 0


# ── jobs ────────────────────────────────────────────────────────────────
def _resolve(reg, persona: str | None, model: str | None) -> tuple[str, str | None]:
    if model:
        if model not in reg.models:
            raise SystemExit(f"unknown model '{model}' — see `law models`")
        return model, None
    persona = persona or reg.raw.get("defaults", {}).get("persona", "general")
    if persona not in reg.personas:
        raise SystemExit(f"unknown persona '{persona}' — see `law models`")
    return reg.personas[persona]["model"], persona


def tail_job(q: Queue, jid: int, out=sys.stdout) -> str:
    """Print a job's progress and answer as it streams. Returns the final status."""
    after, in_reasoning, t0 = 0, False, time.time()
    while True:
        for e in q.events(jid, after):
            after = e["seq"]
            t, d = e["type"], e["data"] or {}
            if t == "deltas":
                for delta in d:
                    r = delta.get("reasoning_content") or delta.get("reasoning")
                    if r:
                        if not in_reasoning:
                            out.write(_dim("[thinking] ")); in_reasoning = True
                        out.write(_dim(r))
                    if delta.get("content"):
                        if in_reasoning:
                            out.write("\n\n"); in_reasoning = False
                        out.write(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        fn = (tc.get("function") or {})
                        if fn.get("name"):
                            out.write(_dim(f"\n[tool call] {fn['name']} "))
                        if fn.get("arguments"):
                            out.write(_dim(fn["arguments"] if isinstance(fn["arguments"], str)
                                           else json.dumps(fn["arguments"])))
                out.flush()
                continue
            if in_reasoning:
                out.write("\n"); in_reasoning = False
            if t == "status":
                st = d.get("state")
                if st == "queued":
                    pos = q.position(jid)
                    out.write(_dim(f"[queued{f', position {pos}' if pos else ''}"
                                   f"{' — ' + d['reason'] if d.get('reason') else ''}]\n"))
                elif st == "running":
                    out.write(_dim(f"[running — attempt {d.get('attempt')}]\n"))
                elif st == "waiting":
                    out.write(_dim(f"[waiting for sub-job(s) {', '.join(map(str, d.get('children') or []))} "
                                   "— they run next; follow them with law jobs]\n"))
            elif t == "model":
                st = d.get("state")
                msg = {"loading": f"loading {d.get('model')} (~{d.get('gb')} GB)"
                                  + (f", context {d['context']}" if d.get("context") else "") + "…",
                       "waiting_ram": f"waiting for memory: {d.get('free_gb')} GB free, "
                                      f"{d.get('model')} needs ~{d.get('need_gb')} GB",
                       "ready": f"{d.get('model')} ready in {d.get('load_s')}s",
                       "unloading": f"unloading {d.get('model')}"}.get(st, st)
                out.write(_dim(f"[{msg}]\n"))
            elif t == "warning":
                out.write(_dim(f"[warning] {d.get('message')}\n"))
            elif t == "probe":
                out.write(_dim(f"[probe] {d.get('message')}\n"))
            elif t == "snapshot":
                out.write(_dim(f"[snapshot {d.get('id')} of {d.get('project')} — undo everything this agent "
                               f"does with: law rollback {d.get('project')} --to {d.get('id')}]\n"))
            elif t == "spec":
                out.write(_cyan(f"[spec] {d.get('title')} → @{d.get('assignee')}  ({d.get('file')})\n"))
                for a in d.get("assumptions") or []:
                    out.write(_dim(f"       assumption: {a}\n"))
                for qq in d.get("questions") or []:
                    out.write(_dim(f"       open question (the agent picks a sensible default): {qq}\n"))
            elif t == "agent":
                out.write(_agent_line(d) + "\n")
            elif t in ("index", "image", "tts", "video"):
                st = d.get("state")
                if st == "progress" and d.get("total"):
                    out.write(_dim(f"[{t} {d.get('done')}/{d.get('total')}] {str(d.get('path', ''))[:60]}\n"))
                elif st and st != "progress":
                    out.write(_dim(f"[{t}: {st}]\n"))
            elif t == "done":
                out.write(_dim(f"\n[done in {time.time() - t0:.0f}s · job {jid}]\n"))
                return "done"
            elif t == "error":
                out.write("\n"); err(f"job {jid} failed: {d.get('message')}")
                return "failed"
            elif t == "cancelled":
                out.write("\n"); warn(f"job {jid} cancelled ({d.get('reason')})")
                return "cancelled"
            out.flush()
        st = q.status(jid)
        if st in TERMINAL and not q.events(jid, after):
            return st
        time.sleep(0.15)


def _worker_warning(q: Queue) -> None:
    w = q.worker_info()
    if not w or w["state"] == "stopped" or w["age_s"] > 30:
        warn("the worker is not running — the job will wait in the queue. Start it: law start")


def cmd_run(a) -> int:
    p = get_paths()
    reg = load(p)
    mid, persona = _resolve(reg, a.persona, a.model)
    msgs = []
    if a.system:
        msgs.append({"role": "system", "content": a.system})
    prompt = " ".join(a.prompt) if a.prompt else sys.stdin.read()
    if not prompt.strip():
        err("empty prompt"); return 2
    msgs.append({"role": "user", "content": prompt})
    q = Queue(p.queue_db)
    jid = q.enqueue(mid, {"messages": msgs, "params": {}}, persona=persona, source="cli")
    print(_dim(f"[job {jid} · {persona or mid} → {mid}]"))
    _worker_warning(q)
    if a.detach:
        print(f"queued. Follow it: law jobs   ·   cancel: law cancel {jid}")
        return 0
    try:
        st = tail_job(q, jid)
    except KeyboardInterrupt:
        q.cancel(jid, reason="Ctrl+C")
        warn(f"cancelled job {jid}")
        return 130
    return 0 if st == "done" else 1


def cmd_index(a) -> int:
    p = get_paths()
    root = str(Path(a.path).expanduser().resolve())
    if not Path(root).is_dir():
        err(f"not a folder: {root}"); return 2
    q = Queue(p.queue_db)
    jid = q.enqueue("qwen3-embed-8b", {"root": root}, source="cli", kind="index")
    print(_dim(f"[job {jid} · index {root}]"))
    _worker_warning(q)
    try:
        st = tail_job(q, jid)
    except KeyboardInterrupt:
        q.cancel(jid, reason="Ctrl+C"); warn(f"cancelled job {jid}"); return 130
    if st == "done":
        res = (json.loads((q.get(jid) or {}).get("result") or "{}")).get("index") or {}
        ok(f"indexed {res.get('files', '?')} files ({res.get('chunks', '?')} chunks) — "
           f"search it with: law search {root} \"<query>\"")
    return 0 if st == "done" else 1


def cmd_search(a) -> int:
    p = get_paths()
    root = str(Path(a.path).expanduser().resolve())
    query = " ".join(a.query) if a.query else ""
    if not query.strip():
        err("empty query — usage: law search <folder> \"<query>\""); return 2
    q = Queue(p.queue_db)
    jid = q.enqueue("qwen3-embed-8b", {"root": root, "query": query, "k": a.k}, source="cli", kind="search")
    print(_dim(f"[job {jid} · search {root}]"))
    _worker_warning(q)
    try:
        st = tail_job(q, jid)
    except KeyboardInterrupt:
        q.cancel(jid, reason="Ctrl+C"); warn(f"cancelled job {jid}"); return 130
    if st != "done":
        return 1
    res = (json.loads((q.get(jid) or {}).get("result") or "{}")).get("results") or []
    if not res:
        warn(f"no results — has this folder been indexed? run: law index {root}")
        return 0
    for r in res:
        print(f"\n{_cyan(str(r['score']))}  {r['path']}:{r['chunk']}")
        print(_dim(" ".join((r.get('text') or '').split())[:300]))
    return 0


def cmd_jobs(a) -> int:
    q = Queue(get_paths().queue_db)
    w = q.worker_info()
    if w:
        print(_dim(f"worker: {w['state']} · model {w['model'] or '-'} · job {w['job_id'] or '-'} · "
                   f"heartbeat {w['age_s']}s ago · {q.depth()} queued"))
    print(f"{'ID':>5}  {'STATUS':<10} {'TRY':>3}  {'KIND':<6} {'MODEL':<18} {'SOURCE':<9} {'AGE':>6}  "
          "PROMPT / ERROR")
    for j in q.jobs(a.n):
        age = time.time() - j["created"]
        age_s = f"{age:.0f}s" if age < 120 else (f"{age / 60:.0f}m" if age < 7200 else f"{age / 3600:.0f}h")
        try:
            last = json.loads(j["payload"])["messages"][-1]["content"]
            last = last if isinstance(last, str) else "[multimodal]"
        except Exception:  # noqa: BLE001
            last = ""
        info = (j["error"] or "") if j["status"] in ("failed", "cancelled") else last
        if j.get("parent_id"):
            info = f"↳ sub-job of {j['parent_id']} (@{j.get('persona')}): {info}"
        info = " ".join(str(info).split())[:70]
        print(f"{j['id']:>5}  {j['status']:<10} {j['attempts']:>3}  {j.get('kind') or 'chat':<6} {j['model']:<18} "
              f"{j['source'] or '':<9} {age_s:>6}  {info}")
    return 0


def cmd_cancel(a) -> int:
    q = Queue(get_paths().queue_db)
    if q.cancel(a.id, reason="cancelled from CLI"):
        kids = [c["id"] for c in q.children(a.id) if c["status"] == "cancelled"]
        ok(f"job {a.id} cancelled" + (f" (and its sub-jobs {', '.join(map(str, kids))})" if kids else ""))
        return 0
    err(f"job {a.id} is not queued/running/waiting (status: {q.status(a.id)})"); return 1


def cmd_logs(a) -> int:
    p = get_paths()
    f = p.logs / (f"{a.name}.log" if a.name in ("worker", "gateway", "watchdog", "telegram") else f"model-{a.name}.log")
    if not f.exists():
        err(f"no log at {f}")
        names = sorted(x.stem for x in p.logs.glob("*.log")) if p.logs.is_dir() else []
        if names:
            print("  available: " + ", ".join(n.replace("model-", "") for n in names))
        return 1
    if a.follow:
        os.execvp("tail", ["tail", "-n", str(a.n), "-f", str(f)])
    lines = f.read_text(errors="replace").splitlines()[-a.n:]
    print("\n".join(lines))
    return 0


def _download(p, m) -> bool:
    py = p.runtime / ".venv" / "bin" / "python"
    if not py.exists():
        err("runtime not installed — run ./install.sh first"); return False
    print(f"downloading {m.id} ({m.repo}, ~{m.weights_gb:g} GB) — safe to interrupt, re-run resumes")
    man = load_manifest(p)
    if man.model(m.repo) is None:
        man.own_model(m.repo, complete=False)
    try:
        System().hf_download(py, m.repo, read_env(p.env_file).get("HF_TOKEN"))
    except Exception as e:  # noqa: BLE001
        err(str(e)); return False
    load_manifest(p).own_model(m.repo, complete=True)
    ok(f"{m.id} downloaded")
    return True


def cmd_pull(a) -> int:
    p = get_paths()
    reg = load(p)
    rc = 0
    for mid in a.ids:
        if mid not in reg.models:
            err(f"unknown model '{mid}' — see `law models`"); rc = 1; continue
        m = reg.models[mid]
        if cached(p, m.repo) and not a.force:
            ok(f"{mid} already downloaded"); continue
        if not _download(p, m):
            rc = 1
    return rc


# ── probe ───────────────────────────────────────────────────────────────
def _sizes(text: str) -> list[int]:
    res = []
    for x in text.split(","):
        x = x.strip().lower()
        if x:
            res.append(int(float(x[:-1]) * 1024) if x.endswith("k") else int(x))
    return res


def cmd_probe(a) -> int:
    from .runtime.probe import REASON_TEXT, load_probes
    p = get_paths()
    reg = load(p)
    if a.show:
        data = load_probes(p)
        ids = a.ids or sorted(data)
        if not data:
            print("no probe results yet — run: law probe <model-id>"); return 0
        for mid in ids:
            r = data.get(mid)
            if not r:
                print(f"{mid}: not probed yet"); continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("ts", 0)))
            proven = r.get("proven_context", r.get("suggested_context"))
            print(f"{mid}  ({when})  load {r.get('load_s')}s · ~{r.get('speed', {}).get('tok_s')} tok/s · "
                  f"first token {r.get('speed', {}).get('ttft_s')}s · proven {proven or 'none'}"
                  + (f" · failed at {r['failed_at']}" if r.get("failed_at") else ""))
            for row in r.get("sizes", []):
                real = f" ({row['prompt_tokens']:,} real)" if row.get("prompt_tokens") else ""
                why = REASON_TEXT.get(row.get("reason") or "", "")
                print(f"    ~{row['size']:>6} tokens{real}  {'OK    ' if row.get('safe') else 'NOT OK'}  "
                      f"{row.get('seconds', '-')}s  recall {'yes' if row.get('recall') else 'no '}  "
                      f"min free {row.get('min_free_gb')} GB  pressure {row.get('pressure')}"
                      + (f"  — {why}" if why else ""))
            if r.get("recommendation"):
                print(f"    verdict: {r['recommendation']}")
        return 0
    if len(a.ids) != 1:
        err("give exactly one model id, e.g. law probe qwen38-27b"); return 2
    mid = a.ids[0]
    if mid not in reg.models:
        err(f"unknown model '{mid}' — see `law models`"); return 2
    if reg.models[mid].kind not in ("lm", "vlm"):
        err(f"{mid} is a '{reg.models[mid].kind}' model — only chat models can be probed"); return 2
    payload = {"messages": [{"role": "user", "content": f"probe {mid}"}], "params": {}}
    if a.sizes:
        try:
            payload["sizes"] = _sizes(a.sizes)
        except ValueError:
            err("--sizes must look like 4k,16k,32768"); return 2
    q = Queue(p.queue_db)
    jid = q.enqueue(mid, payload, source="cli", kind="probe")
    print(_dim(f"[job {jid} · probe {mid}] this loads the model and runs long prompts — it can take a while"))
    _worker_warning(q)
    if a.detach:
        print(f"queued. Results later: law probe --show {mid}"); return 0
    try:
        st = tail_job(q, jid)
    except KeyboardInterrupt:
        q.cancel(jid, reason="Ctrl+C"); warn(f"cancelled job {jid}"); return 130
    if st == "done":
        res = json.loads(q.get(jid)["result"])
        print(res["message"]["content"])
        sug = res["probe"].get("suggested_context")
        if sug and sug != reg.models[mid].context:
            print(f"\nTo apply it, add to ~/.law/config/models.user.yaml:\n"
                  f"models:\n  - id: {mid}\n    context: {sug}\nthen run ./install.sh")
    return 0 if st == "done" else 1


def cmd_webui_reset(a) -> int:
    from .webui import WebUIError, interactive
    try:
        return interactive(a.email)
    except WebUIError as e:
        err(str(e)); return 1
    except (KeyboardInterrupt, EOFError):
        print(); warn("cancelled — nothing changed"); return 130


# ── add / remove models ─────────────────────────────────────────────────
def cmd_add_model(a) -> int:
    from .modelcmds import ModelCmdError, add_model
    p = get_paths()
    try:
        res = add_model(p, a.repo, mid=a.id, kind=a.kind, context=a.context, tool_parser=a.tool_parser,
                        reasoning_parser=a.reasoning_parser, download="auto" if a.auto else "manual",
                        label=a.label, persona=a.persona, force=a.force,
                        token=read_env(p.env_file).get("HF_TOKEN"))
    except ModelCmdError as e:
        err(str(e)); return 1
    e = res["entry"]
    ok(f"added '{e['id']}' → {e['repo']} ({e['kind']}, ~{e['weights_gb']:g} GB, context {e['context']}, "
       f"tool parser {e.get('tool_parser') or '-'}, reasoning parser {e.get('reasoning_parser') or '-'})")
    if a.persona:
        ok(f"persona '{a.persona}' now uses '{e['id']}'")
    for w in res["warnings"]:
        warn(w)
    print(f"  saved in {p.user_models}")
    if a.pull:
        m = load(p).models[e["id"]]
        if not _download(p, m):
            return 1
    else:
        print(f"  next: law pull {e['id']}" + ("   (or ./install.sh — it's set to auto)" if a.auto else ""))
    print(f"  then: ./install.sh   (restarts the services so the gateway lists it)   ·   "
          f"law probe {e['id']}   ·   law run -m {e['id']} \"hi\"")
    return 0


def cmd_remove_model(a) -> int:
    from .modelcmds import ModelCmdError, remove_model
    from .steps.models import _prune
    p = get_paths()
    try:
        res = remove_model(p, a.id)
    except ModelCmdError as e:
        err(str(e)); return 1
    if res["was_override"]:
        ok(f"removed your override of built-in '{a.id}' — its built-in settings apply again")
        print("  next: ./install.sh"); return 0
    ok(f"removed '{a.id}' ({res['repo']}) from your models")
    owned = load_manifest(p).model(res["repo"]) is not None
    if a.delete_files and owned:
        from .engine import Ctx
        _prune(Ctx(paths=p, sys=System()), res["repo"])
        ok(f"moved its files to {p.trash} (deleted after 7 days)")
    elif owned:
        print("  its downloaded files go to the trash on the next ./install.sh (or use --delete-files)")
    elif cached(p, res["repo"]):
        print("  its files were not downloaded by law, so they were left alone")
    print("  next: ./install.sh   (restarts the services)")
    return 0


# ── undo ────────────────────────────────────────────────────────────────
def _undo(folder: str):
    from .undo import Undo
    reg = load(get_paths())
    mb = float(((reg.raw.get("runtime") or {}).get("undo") or {}).get("max_file_mb", 50))
    return Undo(Path(folder), max_file_mb=mb)


def cmd_snapshot(a) -> int:
    from .undo import UndoError, system_snapshot
    try:
        u = _undo(a.dir)
        sid, n, skipped = u.snapshot(a.message or f"manual snapshot {time.strftime('%Y-%m-%d %H:%M')}")
    except UndoError as e:
        err(str(e)); return 1
    if n:
        ok(f"snapshot {sid} of {u.project} ({n} file(s) changed)")
    else:
        ok(f"nothing changed since snapshot {sid} — no new snapshot needed")
    for s in skipped:
        warn(f"skipped (bigger than max_file_mb): {s}")
    if a.system:
        if not is_macos():
            err("--system is macOS only"); return 1
        try:
            ok(f"macOS local snapshot {system_snapshot()} (restore it from Time Machine / Recovery)")
        except UndoError as e:
            err(str(e)); return 1
    return 0


def cmd_reclaim(a) -> int:
    """Reclaim disk space safely — trims the Docker VM (the usual culprit: it balloons from app builds
    and doesn't auto-shrink), prunes unused Docker, cleans uv/brew caches, and optionally old generated
    media. NEVER touches models or databases."""
    import shutil
    import subprocess as sp

    def free_gb() -> float:
        try:
            return shutil.disk_usage("/System/Volumes/Data").free / 1e9
        except OSError:
            return shutil.disk_usage("/").free / 1e9

    before = free_gb()
    print(_dim(f"[free before: {before:.0f} GB]"))
    if shutil.which("colima"):
        print("trimming the Docker VM (fstrim — returns freed space to the host)…")
        try:
            sp.run(["colima", "ssh", "--", "sudo", "fstrim", "-av"], capture_output=True, text=True, timeout=180)
            ok("Docker VM trimmed")
        except (sp.TimeoutExpired, OSError):
            warn("could not trim the Docker VM (is colima running?)")
    if shutil.which("docker"):
        try:
            sp.run(["docker", "system", "prune", "-f"], capture_output=True, text=True, timeout=120)
            ok("pruned unused Docker images/containers")
        except (sp.TimeoutExpired, OSError):
            pass
    for tool, args, label in (("uv", ["cache", "clean"], "uv cache"),
                              ("brew", ["cleanup", "-s"], "Homebrew cache")):
        if shutil.which(tool):
            try:
                sp.run([tool] + args, capture_output=True, text=True, timeout=240)
                ok(f"cleaned {label}")
            except (sp.TimeoutExpired, OSError):
                pass
    if a.media:
        out = get_paths().outputs
        cutoff, removed = time.time() - a.media * 86400, 0
        for sub in ("images", "video", "audio", "vision", "voice-in"):
            d = out / sub
            for f in (d.iterdir() if d.is_dir() else []):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink(); removed += 1
                except OSError:
                    pass
        ok(f"removed {removed} generated file(s) older than {a.media} day(s)")
    after = free_gb()
    ok(f"free: {before:.0f} GB → {after:.0f} GB  (reclaimed ~{max(0, after - before):.0f} GB). "
       "Models were NOT touched — remove those with `law remove-model <id>` if you drop a feature.")
    return 0


def cmd_snapshots(a) -> int:
    from .undo import UndoError
    try:
        u = _undo(a.dir)
        snaps = u.list(a.n)
    except UndoError as e:
        err(str(e)); return 1
    if not snaps:
        print(f"no snapshots yet for {u.project} — take one with: law snapshot {a.dir}")
        return 0
    print(f"snapshots of {u.project} (newest first):")
    for s in snaps:
        print(f"  {s.id}  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.ts))}  {s.message}")
    print("restore one with: law rollback " + (a.dir + " " if a.dir != "." else "") + "--to <id>")
    return 0


def cmd_rollback(a) -> int:
    from .undo import UndoError
    try:
        u = _undo(a.dir)
        res = u.rollback(a.to)
    except UndoError as e:
        err(str(e)); return 1
    if not res["changed"]:
        ok(f"files already match snapshot {res['target']} — nothing to undo")
        return 0
    ok(f"restored {u.project} to snapshot {res['target']} ({len(res['changed'])} file(s))")
    for f in res["changed"][:15]:
        print(f"    {f}")
    if len(res["changed"]) > 15:
        print(f"    … and {len(res['changed']) - 15} more")
    print(f"  changed your mind? law rollback {a.dir} --to {res['safety']}")
    return 0


def add_parsers(sub) -> None:
    s = sub.add_parser("start", help="start the worker + gateway + watchdog services")
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("stop", help="stop all services and unload the model (queue is kept)")
    s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("restart", help="restart the services")
    s.set_defaults(fn=cmd_restart)

    s = sub.add_parser("run", help='queue a prompt and stream the answer (chat, no tools):  law run "hello"')
    s.add_argument("prompt", nargs="*", help="prompt text (or pipe it on stdin)")
    s.add_argument("-p", "--persona", help="persona (default: general)")
    s.add_argument("-m", "--model", help="model id instead of a persona")
    s.add_argument("-s", "--system", help="system prompt")
    s.add_argument("-d", "--detach", action="store_true", help="queue it and return immediately")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("index", help="index a folder's text files for semantic search (RAG, qwen3-embed-8b)")
    s.add_argument("path", help="folder to index")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("search", help='semantic search over an indexed folder:  law search <folder> "query"')
    s.add_argument("path", help="folder previously indexed with `law index`")
    s.add_argument("query", nargs="+", help="what to search for")
    s.add_argument("-k", type=int, default=8, help="number of results (default 8)")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("jobs", help="recent jobs + worker state")
    s.add_argument("-n", type=int, default=20, help="how many jobs to show (default 20)")
    s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("cancel", help="cancel a queued, running or waiting job (and its sub-jobs)")
    s.add_argument("id", type=int, help="job id (from `law jobs`)")
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("logs", help="show logs: worker | gateway | watchdog | telegram | <model id>")
    s.add_argument("name", nargs="?", default="worker", help="worker (default), gateway, watchdog, telegram, or a model id")
    s.add_argument("-n", type=int, default=60, help="number of lines (default 60)")
    s.add_argument("-f", "--follow", action="store_true", help="keep printing new lines (Ctrl+C to stop)")
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("pull", help="download model(s) by id, e.g. law pull deepseek-r1-70b")
    s.add_argument("ids", nargs="+", help="one or more model ids (see `law models`)")
    s.add_argument("--force", action="store_true", help="re-check files even if present")
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("probe", help="measure a model on this Mac: load time, speed, safe context size")
    s.add_argument("ids", nargs="*", help="model id to probe (with --show: models to show, default all)")
    s.add_argument("--sizes", help="context sizes to try, e.g. 4k,16k,32k (default: registry runtime.probe.sizes)")
    s.add_argument("--show", action="store_true", help="show saved results instead of running a probe")
    s.add_argument("-d", "--detach", action="store_true", help="queue it and return immediately")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("webui-reset-password", help="list Open WebUI accounts and set a new password")
    s.add_argument("--email", help="account to reset (default: ask; the only account if there is one)")
    s.set_defaults(fn=cmd_webui_reset)

    s = sub.add_parser("add-model", help="add a Hugging Face model to YOUR models (checks size + RAM limit)")
    s.add_argument("repo", help="Hugging Face repo id, e.g. mlx-community/Qwen3-14B-4bit")
    s.add_argument("--id", help="short id to use (default: derived from the repo name)")
    s.add_argument("--kind", help="lm | vlm | embed | tts | stt (default: guessed from the repo)")
    s.add_argument("--context", type=int, help="context length (default 32768; measure with law probe)")
    s.add_argument("--tool-parser", help="tool-call parser (default: guessed, e.g. qwen3)")
    s.add_argument("--reasoning-parser", help="reasoning parser (default: guessed)")
    s.add_argument("--label", help="description shown in law models")
    s.add_argument("--persona", help="also point this persona at the new model (e.g. general)")
    s.add_argument("--auto", action="store_true", help="download: auto (installer keeps it downloaded)")
    s.add_argument("--pull", action="store_true", help="download it now")
    s.add_argument("--force", action="store_true", help="replace an existing entry / allow over the RAM limit")
    s.set_defaults(fn=cmd_add_model)

    s = sub.add_parser("remove-model", help="remove one of YOUR models (or your override of a built-in)")
    s.add_argument("id", help="model id (see law models)")
    s.add_argument("--delete-files", action="store_true",
                   help="move its downloaded files to the trash now (only if law downloaded them)")
    s.set_defaults(fn=cmd_remove_model)

    s = sub.add_parser("snapshot", help="save an undo point for a project folder")
    s.add_argument("dir", nargs="?", default=".", help="project folder (default: current folder)")
    s.add_argument("-m", "--message", help="what this snapshot is for")
    s.add_argument("--system", action="store_true", help="also take a macOS (APFS) local snapshot")
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("reclaim", help="reclaim disk space (trim Docker VM, prune, clean caches) — never touches models")
    s.add_argument("--media", type=int, default=0, metavar="DAYS",
                   help="also delete generated images/video/audio older than DAYS")
    s.set_defaults(fn=cmd_reclaim)

    s = sub.add_parser("snapshots", help="list undo points of a project folder")
    s.add_argument("dir", nargs="?", default=".", help="project folder (default: current folder)")
    s.add_argument("-n", type=int, default=30, help="how many to show (default 30)")
    s.set_defaults(fn=cmd_snapshots)

    s = sub.add_parser("rollback", help="restore a project folder to a snapshot (itself undoable)")
    s.add_argument("dir", nargs="?", default=".", help="project folder (default: current folder)")
    s.add_argument("--to", help="snapshot id (default: the latest snapshot)")
    s.set_defaults(fn=cmd_rollback)
