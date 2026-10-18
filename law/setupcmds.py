"""`law configure`, `law telegram`, `law setup-opencode`.

law configure
  Interactive (no options): asks for the Telegram bot token (checked with Telegram), your
  Telegram user id (found automatically: you send the bot a message), and an optional
  Hugging Face token. Enter keeps the current value, "-" removes it.
  Non-interactive: --telegram-token, --telegram-user, --hf-token, --telegram-off.
  Saves ~/.law/.env (chmod 600) and then starts / restarts / removes the Telegram service.

law telegram status | test | run
law setup-opencode [--print] [--install]
"""
from __future__ import annotations

import getpass
import json
import sys
from typing import Callable

from .paths import get_paths
from .util import err, is_macos, ok, read_env, warn, write_env, which

ApiFactory = Callable[[str], object]


def _api(token: str):
    from .telegram import Api
    return Api(token)


def _apply_services(no_restart: bool) -> None:
    if no_restart or not is_macos():
        print("  next: ./install.sh (or law start) to apply")
        return
    from .engine import Ctx, StepFailed, run_steps
    from .steps import services
    from .system import System
    try:
        run_steps(Ctx(paths=get_paths(), sys=System()), services.STEPS, on_macos=True)
    except StepFailed:
        err("could not update the background services — run ./install.sh")


def check_token(token: str, api_factory: ApiFactory = _api) -> str:
    """→ bot username. Raises ValueError with a readable reason."""
    from .telegram import TGError
    try:
        me = api_factory(token).call("getMe") or {}
    except TGError as e:
        raise ValueError("Telegram rejected this token — copy it again from @BotFather" if e.code in (401, 404)
                         else str(e)) from e
    except OSError as e:
        raise ValueError(f"cannot reach Telegram ({e})") from e
    return me.get("username") or "?"


def detect_user(token: str, api_factory: ApiFactory = _api) -> list[tuple[int, str]]:
    """Users who recently messaged the bot: [(id, name)]."""
    ups = api_factory(token).get_updates(None, 0)
    seen: dict[int, str] = {}
    for u in ups:
        frm = (u.get("message") or {}).get("from") or {}
        if frm.get("id") is not None:
            seen[int(frm["id"])] = " ".join(x for x in (frm.get("first_name"), frm.get("username") and
                                                        "@" + frm["username"]) if x)
    return list(seen.items())


def _stop_bot() -> None:
    if is_macos():
        from .system import System
        System().launchd_unload("com.law.telegram")


def interactive(ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass,
                api_factory: ApiFactory = _api, out=sys.stdout) -> dict:
    """Returns the changes {KEY: value or None (= remove)}."""
    from .telegram import mask
    env = read_env(get_paths().env_file)
    changes: dict = {}
    out.write("Telegram lets you send tasks from your phone. Create a bot with @BotFather "
              "(/newbot) and copy its token.\n")
    tok = secret(f"Telegram bot token [{mask(env.get('TELEGRAM_BOT_TOKEN'))}] "
                 "(Enter = keep, - = remove): ").strip()
    token = env.get("TELEGRAM_BOT_TOKEN")
    if tok == "-":
        changes.update(TELEGRAM_BOT_TOKEN=None, TELEGRAM_USER_ID=None)
        token = None
    elif tok:
        name = check_token(tok, api_factory)
        out.write(f"  ✓ token works — bot @{name}\n")
        changes["TELEGRAM_BOT_TOKEN"] = token = tok
    if token:
        cur = env.get("TELEGRAM_USER_ID") or ""
        uid = ask(f"Your Telegram user id [{cur or 'not set'}] (Enter = keep / find it, - = remove): ").strip()
        if uid == "-":
            changes["TELEGRAM_USER_ID"] = None
        elif uid:
            if not all(p.strip().lstrip("-").isdigit() for p in uid.split(",")):
                raise ValueError("a Telegram user id is a number (several: comma-separated)")
            changes["TELEGRAM_USER_ID"] = uid.replace(" ", "")
        elif not cur:
            _stop_bot()              # a running bot would read the message first
            ask("Send any message to your bot in Telegram now, then press Enter here… ")
            found = detect_user(token, api_factory)
            if not found:
                raise ValueError("no message reached the bot — send it a message and run `law configure` again")
            for i, (fid, name) in enumerate(found, 1):
                out.write(f"  {i}. {fid}  {name}\n")
            pick = ask("Which one is you? [1]: ").strip() or "1"
            changes["TELEGRAM_USER_ID"] = str(found[int(pick) - 1][0])
    hf = secret(f"Hugging Face token (only for gated models) [{mask(env.get('HF_TOKEN'))}] "
                "(Enter = keep, - = remove): ").strip()
    if hf == "-":
        changes["HF_TOKEN"] = None
    elif hf:
        changes["HF_TOKEN"] = hf
    return changes


def save(changes: dict) -> list[str]:
    p = get_paths()
    env = read_env(p.env_file)
    done = []
    for k, v in changes.items():
        if v is None:
            if k in env:
                env.pop(k)
                done.append(f"removed {k}")
        elif env.get(k) != v:
            env[k] = v
            done.append(f"set {k}")
    p.law.mkdir(parents=True, exist_ok=True)
    write_env(p.env_file, env)
    return done


def cmd_configure(a, api_factory: ApiFactory = _api) -> int:
    changes: dict = {}
    try:
        if a.telegram_off:
            changes.update(TELEGRAM_BOT_TOKEN=None, TELEGRAM_USER_ID=None)
        if a.telegram_token:
            name = check_token(a.telegram_token, api_factory)
            ok(f"Telegram token works — bot @{name}")
            changes["TELEGRAM_BOT_TOKEN"] = a.telegram_token
        if a.telegram_user:
            if not all(x.strip().lstrip("-").isdigit() for x in a.telegram_user.split(",")):
                err("--telegram-user must be a number (several: comma-separated)"); return 2
            changes["TELEGRAM_USER_ID"] = a.telegram_user.replace(" ", "")
        if a.hf_token:
            changes["HF_TOKEN"] = a.hf_token
        if not changes:
            if not sys.stdin.isatty():
                err("nothing to change (run `law configure` in a terminal, or pass options — see --help)")
                return 2
            changes = interactive(api_factory=api_factory)
    except ValueError as e:
        err(str(e)); return 1
    except (KeyboardInterrupt, EOFError):
        print(); warn("cancelled — nothing changed"); return 130
    done = save(changes)
    for d in done:
        ok(d)
    if not done:
        ok("nothing changed")
    from .telegram import configured
    p = get_paths()
    if any(k.startswith("TELEGRAM") for k in changes) or done:
        _apply_services(a.no_restart)
    if configured(p):
        print("  Telegram: send /help to your bot.")
    elif read_env(p.env_file).get("TELEGRAM_BOT_TOKEN"):
        warn("Telegram needs both the bot token and your user id — the bot is not running yet")
    return 0


# ── law telegram ────────────────────────────────────────────────────────
def cmd_telegram(a, api_factory: ApiFactory = _api) -> int:
    from .telegram import Bot, beat_age, creds, mask
    p = get_paths()
    token, users = creds(p)
    action = a.action or "status"
    if not token or not users:
        print("Telegram is not configured — run: law configure")
        return 1 if action != "status" else 0
    if action == "status":
        try:
            name = check_token(token, api_factory)
            ok(f"bot @{name} · token {mask(token)} · allowed user id(s): {', '.join(map(str, sorted(users)))}")
        except ValueError as e:
            err(str(e))
        if is_macos():
            from .system import System
            print(f"  service com.law.telegram: {'running' if System().launchd_loaded('com.law.telegram') else 'NOT running → law start'}")
        age = beat_age(p)
        print(f"  last poll: {'never' if age is None else f'{age:.0f}s ago'}")
        try:
            st = json.loads((p.state / "telegram.json").read_text())
            print(f"  following {len(st.get('follows') or {})} job(s) · {len(st.get('chats') or {})} chat(s)")
        except (OSError, ValueError):
            pass
        print("  log: law logs telegram")
        return 0
    if action == "test":
        api = api_factory(token)
        bad = 0
        for u in sorted(users):
            try:
                api.call("sendMessage", chat_id=u, text="✅ law test message — the workstation can reach you.")
                ok(f"sent a test message to {u}")
            except Exception as e:  # noqa: BLE001
                bad += 1
                err(f"could not message {u}: {e} — open the bot in Telegram and press Start first")
        return 1 if bad else 0
    if action == "run":
        if is_macos():
            from .system import System
            if System().launchd_loaded("com.law.telegram"):
                err("the Telegram service is running — stop it first (launchctl bootout gui/$UID/com.law.telegram)")
                return 1
        import logging

        from .registry import load
        from .runtime.db import Queue
        logging.basicConfig(level=logging.INFO, format="%(asctime)s telegram %(levelname)s %(message)s")
        bot = Bot(p, load(p), Queue(p.queue_db), api_factory(token), users)
        try:
            bot.run_forever()
        except KeyboardInterrupt:
            bot.stop_event.set()
        return 0
    err(f"unknown action {action}"); return 2


# ── law setup-opencode ──────────────────────────────────────────────────
def cmd_setup_opencode(a) -> int:
    from . import opencode
    from .registry import load
    from .util import run
    p = get_paths()
    reg = load(p)
    if a.print:
        print(json.dumps({"provider": {opencode.PROVIDER_ID: opencode.provider_block(reg)},
                          "model": "law/" + ((reg.raw.get("runtime") or {}).get("opencode") or {})
                          .get("default_model", "coder")}, indent=2))
        return 0
    if a.install and not which("opencode"):
        brew = which("brew")
        if not brew:
            err("Homebrew not found — run ./install.sh first"); return 1
        print("installing OpenCode with Homebrew…")
        if run([brew, "install", "opencode"], check=False, capture=False, timeout=3600).returncode != 0:
            err("`brew install opencode` failed. Install it another way, e.g. "
                "`curl -fsSL https://opencode.ai/install | bash`, then run law setup-opencode again")
            return 1
    try:
        changed, want = opencode.needs_update(p.home, reg)
    except opencode.JsoncConfig as e:
        err(str(e)); return 1
    if changed:
        f = opencode.write(p.home, want)
        ok(f"OpenCode provider 'law' written to {f}")
    else:
        ok("OpenCode is already set up for law")
    print(f"  models: {', '.join('law/' + m for m in want['provider']['law']['models'])}")
    print(f"  default model: {want.get('model')}")
    if not which("opencode"):
        warn("OpenCode itself is not installed — law setup-opencode --install (or see opencode.ai)")
    else:
        print("  use it:  cd <project> && opencode     (pick a model with /models)")
    return 0


# ── law setup-music (ACE-Step) ────────────────────────────────────────────
def cmd_setup_music(a) -> int:
    """Install ACE-Step music generation: clone ~/ace-step, uv sync, launch the service on :7860.
    Separate from `law install` because it is a heavy optional download (~5 GB models on first use)."""
    from .engine import Ctx, StepFailed, run_steps
    from .steps import acestep
    from .system import System
    try:
        run_steps(Ctx(paths=get_paths(), sys=System()), acestep.STEPS, on_macos=True)
    except StepFailed:
        warn("ACE-Step setup hit a problem — see the output above")
        return 1
    ok("ACE-Step music studio set up → http://localhost:7860 (first generation downloads ~5 GB)")
    return 0


def add_parsers(sub) -> None:
    s = sub.add_parser("configure", help="set secrets: Telegram bot token + user id, Hugging Face token")
    s.add_argument("--telegram-token", help="bot token from @BotFather (checked with Telegram)")
    s.add_argument("--telegram-user", help="your Telegram user id (several: comma-separated)")
    s.add_argument("--hf-token", help="Hugging Face token (only needed for gated models)")
    s.add_argument("--telegram-off", action="store_true", help="remove the Telegram secrets and stop the bot")
    s.add_argument("--no-restart", action="store_true", help="only save; don't start/restart services")
    s.set_defaults(fn=cmd_configure)

    s = sub.add_parser("telegram", help="Telegram bot: status (default) | test | run")
    s.add_argument("action", nargs="?", choices=["status", "test", "run"], help="what to do (default: status)")
    s.set_defaults(fn=cmd_telegram)

    s = sub.add_parser("setup-music", help="install ACE-Step music generation (clone + deps + service on :7860)")
    s.set_defaults(fn=cmd_setup_music)

    s = sub.add_parser("setup-opencode", help="point OpenCode at the law gateway (models law/coder, law/general…)")
    s.add_argument("--print", action="store_true", help="only print the provider block (for opencode.jsonc)")
    s.add_argument("--install", action="store_true", help="also install OpenCode with Homebrew if missing")
    s.set_defaults(fn=cmd_setup_opencode)

