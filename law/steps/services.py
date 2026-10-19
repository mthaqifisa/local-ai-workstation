"""Background services (launchd, start at login, restarted if they crash):

  com.law.worker   — the single job runner (owns the model process)
  com.law.gateway  — OpenAI + Anthropic compatible API on the gateway port
  com.law.dashboard— live read-only web dashboard on the dashboard port (watch jobs think)
  com.law.watchdog — restarts the worker / gateway / bot if they freeze or stall
  com.law.telegram — the Telegram bot; ONLY while TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID are set
                     (remove them with `law configure --telegram-off` and the service is removed)

A service is (re)loaded when its plist changes — and the plist embeds the law version and
a hash of the resolved registry, so upgrading law or editing models restarts it.
com.law.* services that are no longer defined are stopped and removed.
"""
from __future__ import annotations

import hashlib
import os
import sys
from xml.sax.saxutils import escape

from .. import __version__
from ..engine import Action, Ctx, Step
from ..registry import load
from ..util import atomic_write
from .base import expected_generated

SERVICES = {"worker": "law.runtime.worker", "gateway": "law.runtime.gateway",
            "dashboard": "law.runtime.dashboard", "watchdog": "law.runtime.watchdog"}
KEEP = {"com.law.colima",                      # managed by the docker step
        "com.law.acestep"}                     # optional music service, managed by `law setup-music`


def label(name: str) -> str:
    return f"com.law.{name}"


def plist_body(ctx: Ctx, name: str, module: str, regsha: str, extra: dict | None = None) -> str:
    p = ctx.paths
    env = {"HOME": str(p.home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "PYTHONUNBUFFERED": "1", "LAW_VERSION": __version__, "LAW_REGISTRY_SHA": regsha, **(extra or {})}
    for k in ("LAW_HOME", "HF_HOME", "HF_HUB_CACHE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    # LAN opt-in: if the user set LAW_BIND_HOST in ~/.law/.env, pass it to the services (the gateway +
    # dashboard read it to decide what to bind to). Not set → services bind to 127.0.0.1 (safe default).
    from ..util import read_env
    bind = (read_env(p.env_file).get("LAW_BIND_HOST") or "").strip()
    if bind:
        env["LAW_BIND_HOST"] = bind
    env_xml = "".join(f"    <key>{k}</key><string>{escape(v)}</string>\n" for k, v in sorted(env.items()))
    log = escape(str(p.logs / f"{name}.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label(name)}</string>
  <key>ProgramArguments</key><array>
    <string>{escape(sys.executable)}</string><string>-m</string><string>{module}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
{env_xml}  </dict>
  <key>WorkingDirectory</key><string>{escape(str(p.law))}</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ExitTimeOut</key><integer>45</integer>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
"""


def wanted(ctx: Ctx) -> dict[str, tuple]:
    regsha = hashlib.sha256(expected_generated(ctx)["registry.resolved.json"].encode()).hexdigest()[:12]
    la = ctx.paths.launch_agents
    out = {label(n): (la / f"{label(n)}.plist", plist_body(ctx, n, mod, regsha))
           for n, mod in SERVICES.items()}
    from ..telegram import creds
    token, users = creds(ctx.paths)
    if token and users:
        # the secrets' hash is part of the plist, so a new token / user id restarts the bot
        tsha = hashlib.sha256(f"{token}|{sorted(users)}".encode()).hexdigest()[:12]
        out[label("telegram")] = (la / f"{label('telegram')}.plist",
                                  plist_body(ctx, "telegram", "law.telegram", regsha, {"LAW_TELEGRAM_SHA": tsha}))
    return out


def plan_services(ctx: Ctx) -> list[Action]:
    want = wanted(ctx)
    acts: list[Action] = []
    for lab, (f, body) in want.items():
        if not f.exists() or f.read_text() != body:
            def load_(lab=lab, f=f, body=body):
                atomic_write(f, body)
                ctx.sys.launchd_load(lab, f)
            acts.append(Action("update" if f.exists() else "add", f"service {lab}", load_))
        elif not ctx.sys.launchd_loaded(lab):
            acts.append(Action("add", f"start service {lab}", lambda lab=lab, f=f: ctx.sys.launchd_load(lab, f)))
    la = ctx.paths.launch_agents
    if la.is_dir():
        for f in sorted(la.glob("com.law.*.plist")):
            lab = f.stem
            if lab not in want and lab not in KEEP:
                def rm(lab=lab, f=f):
                    ctx.sys.launchd_unload(lab)
                    f.unlink(missing_ok=True)
                acts.append(Action("remove", f"service {lab} (no longer used)", rm))
    if not acts:
        reg = load(ctx.paths)
        url = f"http://{reg.local_host}:{reg.ports['gateway']}/health"
        if not ctx.sys.http_ok(url):
            acts.append(Action("update", f"restart gateway (no answer from {url}; log: ~/.law/logs/gateway.log)",
                               lambda: ctx.sys.launchd_kickstart(label("gateway"))))
    return acts


STEPS = [Step("services", "Background services: worker + gateway + watchdog (+ Telegram)", plan_services,
              macos_only=True)]
