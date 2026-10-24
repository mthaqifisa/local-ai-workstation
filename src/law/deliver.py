"""Send finished files to the owner: to Tony's Telegram chats and/or by email (Mail.app). Agents run it as `law send`.

Telegram uses the bot's own token and allow-list from ~/LAW/state/.env, so a file only ever reaches the owner's
allow-listed chats (a private chat's id is the user's id). Email goes through law.mailer (the Mac's Mail account).
"""

from __future__ import annotations

from pathlib import Path

import httpx

from law import mailer, secrets

API = "https://api.telegram.org"
CAPTION_LIMIT = 1024  # Telegram's limit for a document caption
MAX_FILE_BYTES = 50 * 1024 * 1024  # what the Bot API accepts per upload


class DeliveryError(RuntimeError):
    pass


def check_files(files: list[Path]) -> None:
    for path in files:
        if not path.is_file():
            raise DeliveryError(f"file not found: {path}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise DeliveryError(f"{path.name} is over Telegram's 50 MB upload limit")


def to_telegram(env_file: Path, files: list[Path], message: str = "",
                transport: httpx.BaseTransport | None = None) -> int:
    """Send each file as a document (full quality) to every allow-listed chat; `message` captions the first file
    (or is sent alone without files). Returns the number of chats reached."""
    values = secrets.read(env_file)
    token = values.get("LAW_TELEGRAM_TOKEN", "")
    chats = sorted(secrets.allowed_telegram_ids(values))
    if not token or not chats:
        raise DeliveryError("Telegram is not set up: LAW_TELEGRAM_TOKEN and LAW_TELEGRAM_USER_IDS are needed")
    check_files(files)
    with httpx.Client(base_url=f"{API}/bot{token}", timeout=300, transport=transport) as client:
        for chat in chats:
            if not files:
                _ok(client.post("/sendMessage", data={"chat_id": str(chat), "text": message[:4096]}))
            for n, path in enumerate(files):
                data = {"chat_id": str(chat)}
                if n == 0 and message:
                    data["caption"] = message[:CAPTION_LIMIT]
                with path.open("rb") as fh:
                    _ok(client.post("/sendDocument", data=data, files={"document": (path.name, fh)}))
    return len(chats)


def _ok(response: httpx.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("ok"):
        # never echo the request URL: it holds the bot token
        raise DeliveryError(f"Telegram refused the upload: {body.get('description') or response.status_code}")


def by_email(env_file: Path, work: Path, files: list[Path], subject: str, message: str, to: str | None = None) -> str:
    """Email the files through Mail.app to `to`, or to the owner's report address. Returns the address."""
    check_files(files)
    try:
        address = (to or "").strip() or mailer.recipient(env_file)
        if not mailer.valid(address):
            raise DeliveryError(f"not an email address: {address}")
        mailer.send(address, subject, message or subject, files, work)
    except mailer.MailError as exc:
        raise DeliveryError(str(exc)) from exc
    return address
