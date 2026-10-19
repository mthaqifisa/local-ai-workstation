"""Interactive Telegram setup used by `./install.sh` / `law install` when no bot is configured.

Asks for the bot token (hidden input), confirms it with Telegram, then finds the user's numeric ID by
reading who just messaged the bot. Every prompt can be skipped with Enter.
"""

from __future__ import annotations

import getpass
import re
from collections.abc import Callable
from typing import Any

import httpx

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
API = "https://api.telegram.org"

Ask = Callable[[str], str]
Say = Callable[[str], None]


def get_me(token: str) -> dict[str, Any] | None:
    """The bot's identity, or None if Telegram rejects the token."""
    try:
        r = httpx.get(f"{API}/bot{token}/getMe", timeout=15)
    except httpx.HTTPError:
        return None
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    result: dict[str, Any] | None = data.get("result") if data.get("ok") else None
    return result


def recent_senders(token: str) -> list[tuple[int, str]]:
    """(user id, display name) of people who messaged the bot recently, newest first."""
    try:
        r = httpx.get(f"{API}/bot{token}/getUpdates", params={"timeout": 0, "limit": 100}, timeout=20)
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    if not data.get("ok"):
        return []
    seen: dict[int, str] = {}  # insertion order = newest sender first
    for update in reversed(data.get("result", [])):
        sender = (update.get("message") or update.get("edited_message") or {}).get("from") or {}
        if not sender.get("id") or sender.get("is_bot"):
            continue
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x)
        label = f"{name} (@{sender['username']})" if sender.get("username") else name
        if sender["id"] not in seen or ("@" in label and "@" not in seen[sender["id"]]):
            seen[sender["id"]] = label  # keep the most informative name seen for this person
    return list(seen.items())


def configure(ask: Ask = input, ask_secret: Ask = getpass.getpass, say: Say = print) -> dict[str, str]:
    """Walk the user through Telegram setup. Returns {} if skipped, else the values to store."""
    say("\nTelegram setup (the main way to talk to LAW). Press Enter at any prompt to skip for now.")
    say("Get a token from @BotFather in Telegram: /newbot for a new bot, or /mybots -> your bot -> API Token.")
    for _ in range(3):
        token = ask_secret("Bot token (input hidden): ").strip()
        if not token:
            say("Skipped. Run ./install.sh again any time to set up Telegram.")
            return {}
        if not TOKEN_RE.match(token):
            say("That doesn't look like a bot token (it should look like 123456789:AAH...). Try again.")
            continue
        bot = get_me(token)
        if bot is None:
            say("Telegram rejected that token (or there is no internet). Check it and try again.")
            continue
        say(f"Connected to @{bot.get('username')} ({bot.get('first_name')}).")
        ids = _choose_ids(token, str(bot.get("username")), ask, say)
        if not ids:
            say("No user allowed, so the bot would ignore everyone. Skipping Telegram for now.")
            return {}
        return {"LAW_TELEGRAM_TOKEN": token, "LAW_TELEGRAM_USER_IDS": ",".join(str(i) for i in ids)}
    say("Too many attempts. Skipping Telegram for now.")
    return {}


def _choose_ids(token: str, username: str, ask: Ask, say: Say) -> list[int]:
    say(f"\nNow open @{username} in Telegram on your phone or Mac and send it any message (e.g. 'hi').")
    ask("Press Enter after you've sent it (or type 'm' to enter user IDs manually): ")
    for _ in range(2):
        senders = recent_senders(token)
        if senders:
            for n, (uid, name) in enumerate(senders, 1):
                say(f"  {n}. {name}  [id {uid}]")
            pick = ask("Allow which? Numbers separated by commas (Enter = 1): ").strip() or "1"
            chosen = [senders[int(p) - 1][0] for p in re.split(r"[,\s]+", pick)
                      if p.isdigit() and 1 <= int(p) <= len(senders)]
            if chosen:
                return chosen
        else:
            say("No messages found yet. If another program is still running this bot, stop it first.")
            if ask("Press Enter to check again, or 'm' to type IDs: ").strip().lower() != "m":
                continue
        break
    raw = ask("Numeric Telegram user ID(s), comma-separated (from @userinfobot): ").strip()
    return [int(x) for x in re.split(r"[,\s]+", raw) if x.isdigit()]
