"""Telegram bot: the primary channel. Thin adapter over the LAW job API.

Internet-reachable, so it answers only the allow-listed numeric user IDs in ~/LAW/state/.env
(LAW_TELEGRAM_USER_IDS). This is an allow-list, not a login.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from law import registry, secrets
from law.paths import LawPaths
from law.tg_format import to_telegram_html

log = logging.getLogger("law.telegram")


class LawBot:
    def __init__(self, token: str, allowed: set[int], api_base: str, delivered: Path | None = None,
                 notify_sources: set[str] | None = None) -> None:
        self.bot = Bot(token)
        self.dp = Dispatcher()
        self.allowed = allowed
        self.api = api_base.rstrip("/")
        self.delivered = Delivered(delivered or LawPaths.from_env().state / "telegram-delivered.txt")
        self._tasks: set[asyncio.Task[None]] = set()
        self.notify_sources = notify_sources or set()
        self._wire()

    def _ok(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in self.allowed

    def _wire(self) -> None:
        dp = self.dp

        @dp.message(Command("start", "help"))
        async def help_(message: Message) -> None:
            if self._ok(message):
                await self._send(message.chat.id, HELP_TEXT)

        @dp.message(Command("ask"))
        async def ask(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "ask", {"prompt": command.args, "role": "assistant"})

        @dp.message(Command("code"))
        async def code(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "task", {"role": "coder", **split_dir(command.args)})

        @dp.message(Command("agent"))
        async def agent(message: Message, command: CommandObject) -> None:
            if not self._ok(message):
                return
            parts = (command.args or "").split(maxsplit=1)
            if len(parts) < 2:
                await self._send(message.chat.id, "Usage: `/agent <role> <task>` — see /roles")
                return
            await self._job(message, "task", {"role": parts[0], **split_dir(parts[1])})

        @dp.message(Command("image"))
        async def image(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "image", {"prompt": command.args}, file_field="image")

        @dp.message(Command("video"))
        async def video(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "video", {"prompt": command.args}, file_field="video")

        @dp.message(Command("film"))
        async def film(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "film", {"prompt": command.args}, file_field="video")

        @dp.message(Command("music"))
        async def music(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "song", {"prompt": command.args, "duration": 60}, file_field="audio")

        @dp.message(Command("say"))
        async def say(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "tts", {"text": command.args}, file_field="voice")

        @dp.message(Command("search"))
        async def search(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "ask", {"prompt": f"Search the web and answer with sources: {command.args}",
                                                 "role": "researcher"})

        @dp.message(Command("roles"))
        async def roles_(message: Message) -> None:
            if self._ok(message):
                await self._send(message.chat.id, roles_text())

        @dp.message(Command("jobs"))
        async def jobs(message: Message) -> None:
            if not self._ok(message):
                return
            async with httpx.AsyncClient(timeout=10) as client:
                data = (await client.get(f"{self.api}/jobs", params={"limit": 30})).json()
            rows = [j for j in data if j.get("parent") is None][:10]
            lines = [f"`{j['id']}` {j['kind']} — {j['status']}" for j in rows]
            await self._send(message.chat.id, "**Recent jobs**\n" + ("\n".join(lines) or "none") +
                             "\n\nStop one with `/cancel <id>`.")

        @dp.message(Command("cancel"))
        async def cancel(message: Message, command: CommandObject) -> None:
            if not self._ok(message):
                return
            job_id = (command.args or "").strip()
            async with httpx.AsyncClient(timeout=10) as client:
                if not job_id:  # newest unfinished job
                    data = (await client.get(f"{self.api}/jobs", params={"limit": 30})).json()
                    live = [j for j in data if j.get("parent") is None and j["status"] in LIVE]
                    job_id = live[0]["id"] if live else ""
                if not job_id:
                    await self._send(message.chat.id, "Nothing is running.")
                    return
                r = await client.post(f"{self.api}/jobs/{job_id}/cancel")
            reply = f"Cancelling `{job_id}`." if r.status_code == 200 else f"No job `{job_id}`."
            await self._send(message.chat.id, reply)

        @dp.message(F.voice | F.audio)
        async def voice_note(message: Message) -> None:
            if not self._ok(message):
                return
            media = message.voice or message.audio
            if media is None:
                return
            path = await self._download(media.file_id, "ogg", "audio")
            await self._job(message, "stt", {"audio": path}, then="ask")

        @dp.message(F.photo)
        async def photo(message: Message) -> None:
            if not self._ok(message) or not message.photo:
                return
            path = await self._download(message.photo[-1].file_id, "jpg", "images")
            caption = (message.caption or "").strip()
            verb, _, rest = caption.partition(" ")
            if verb in {"/edit", "/animate"} and not rest.strip():
                await self._send(message.chat.id, f"Add what to do after `{verb}`, e.g. `{verb} make the sky stormy`.")
            elif verb == "/edit":
                await self._job(message, "edit", {"image": path, "prompt": rest.strip()}, file_field="image")
            elif verb == "/animate":
                await self._job(message, "animate", {"prompt": rest.strip(), "image": path}, file_field="video")
            else:
                await self._job(message, "ask", {"prompt": caption or "Describe this image.", "role": "assistant",
                                                 "image": path})

        @dp.message(F.text)
        async def text(message: Message) -> None:
            if not self._ok(message) or not message.text:
                return
            if message.text.startswith("/"):
                await self._send(message.chat.id, "Unknown command. Send /help for the list.")
                return
            await self._job(message, "ask", {"prompt": message.text, "role": "assistant"})

    async def _download(self, file_id: str, ext: str, folder: str) -> str:
        file = await self.bot.get_file(file_id)
        if not file.file_path:
            raise RuntimeError("Telegram returned no file path")
        dest = LawPaths.from_env().outputs / folder / "telegram" / f"tg-{file_id[:16]}.{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        await self.bot.download_file(file.file_path, str(dest))
        return str(dest)

    async def _job(self, message: Message, kind: str, payload: dict[str, Any], *,
                   file_field: str | None = None, then: str | None = None) -> None:
        """Submit a job that remembers where to reply, then deliver the result (resumable after restarts)."""
        status = await message.answer("⏳ queued")
        reply = {"chat_id": message.chat.id, "status_id": status.message_id, "file_field": file_field, "then": then}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{self.api}/jobs", json={"kind": kind, "payload": payload,
                                                            "source": "telegram", "reply_to": reply})
        if r.status_code != 201:
            await self._edit(reply, f"error: {r.text[:200]}")
            return
        job_id = r.json()["id"]
        ahead = await self._ahead_of(job_id)
        if ahead:
            await self._edit(reply, f"⏳ queued: {ahead} job{'s' if ahead > 1 else ''} ahead")
        await self._deliver(job_id, reply)

    async def _ahead_of(self, job_id: str) -> int:
        """Top-level jobs already running or waiting that this one queues behind."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                jobs = (await client.get(f"{self.api}/jobs", params={"limit": 100})).json()
        except (httpx.HTTPError, ValueError):
            return 0
        return queued_ahead(jobs, job_id)

    async def _deliver(self, job_id: str, reply: dict[str, Any]) -> None:
        if self.delivered.seen(job_id):
            return
        last = ""
        while True:  # stream progress; reconnect and poll if the API restarts
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as client, \
                        client.stream("GET", f"{self.api}/jobs/{job_id}/stream") as stream:
                    async for line in stream.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        ev = json.loads(line[6:])
                        if ev.get("event") == "started" and not last:
                            await self._edit(reply, "working…")
                        msg = ev.get("data", {}).get("message")
                        if ev.get("event") == "progress" and msg and msg != last:
                            last = msg
                            await self._edit(reply, f"… {msg}")
            except httpx.HTTPError:
                pass
            final = await self._get(job_id)
            if final and final.get("status") in TERMINAL:
                break
            await asyncio.sleep(3)
        await self._send_result(final, reply)
        self.delivered.add(job_id)

    async def _send_result(self, final: dict[str, Any], reply: dict[str, Any]) -> None:
        from aiogram.types import FSInputFile

        chat = reply["chat_id"]
        result = final.get("result") or {}
        if final.get("status") != "done":
            await self._edit(reply, outcome_line(final))
            return
        if reply.get("then") == "ask" and result.get("text"):
            await self._edit(reply, f"heard: {result['text'][:300]}")
            async with httpx.AsyncClient(timeout=30) as client:
                status = await self.bot.send_message(chat, "working…")
                follow = {"chat_id": chat, "status_id": status.message_id, "file_field": None, "then": None}
                r = await client.post(f"{self.api}/jobs", json={
                    "kind": "ask", "payload": {"prompt": result["text"], "role": "assistant"},
                    "source": "telegram", "reply_to": follow})
            if r.status_code == 201:
                await self._deliver(r.json()["id"], follow)
            return
        field = reply.get("file_field")
        source = result.get("audio") if field == "voice" else result.get(field) if field else None
        if field and source:
            path = FSInputFile(source)
            await self._edit(reply, "done")
            if field == "image":
                await self.bot.send_photo(chat, path)
            elif field == "video":
                await self.bot.send_video(chat, path)
            elif field == "voice":
                await self.bot.send_audio(chat, path, title="Tony")
            else:
                await self.bot.send_audio(chat, path)
            return
        text = str(result.get("answer") or result.get("text") or "done")
        parts = split_message(text, limit=3500)  # split Markdown first; HTML tags add length
        await self._edit(reply, parts[0], rich=True)
        for part in parts[1:]:
            await self._send(chat, part)

    async def _send(self, chat: int, markdown: str) -> None:
        try:
            await self.bot.send_message(chat, to_telegram_html(markdown), parse_mode="HTML",
                                        disable_web_page_preview=True)
        except Exception:  # Telegram rejected the markup: never lose the answer, send it plain
            await self.bot.send_message(chat, markdown)

    async def _edit(self, reply: dict[str, Any], text: str, *, rich: bool = False) -> None:
        chat, msg = reply["chat_id"], reply["status_id"]
        try:
            if rich:
                await self.bot.edit_message_text(to_telegram_html(text), chat_id=chat, message_id=msg,
                                                 parse_mode="HTML", disable_web_page_preview=True)
            else:
                await self.bot.edit_message_text(text, chat_id=chat, message_id=msg)
        except Exception:  # identical text, bad markup, deleted or too-old message: send a new one
            if rich:
                await self._send(chat, text)
            else:
                await self.bot.send_message(chat, text)

    async def _get(self, job_id: str) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{self.api}/jobs/{job_id}")
                r.raise_for_status()
                job: dict[str, Any] = r.json()
                return job
        except httpx.HTTPError:
            return None

    async def resume(self) -> None:
        """After a restart, deliver replies for Telegram jobs from the last day that were never delivered."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                jobs = (await client.get(f"{self.api}/jobs", params={"limit": 200})).json()
        except (httpx.HTTPError, ValueError):
            return
        cutoff = time.time() - 86400
        for job in jobs:
            if (job.get("source") == "telegram" and job.get("reply_to") and job.get("parent") is None
                    and job["created"] > cutoff and not self.delivered.seen(job["id"])):
                task = asyncio.create_task(self._deliver(job["id"], job["reply_to"]))
                self._tasks.add(task)  # keep a reference so the task isn't garbage-collected
                task.add_done_callback(self._tasks.discard)

    async def watch_other_jobs(self, poll_s: float = 5.0) -> None:
        """Report jobs started outside Telegram (CLI, Console, API) to every allowed user: start and result."""
        started_at = time.time() - 60
        while True:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    jobs = (await client.get(f"{self.api}/jobs", params={"limit": 50})).json()
                for job in reversed(jobs):
                    if (job.get("parent") is None and job.get("source") in self.notify_sources
                            and (job["created"] >= started_at or job["status"] in LIVE)):
                        await self._notify(job)
            except (httpx.HTTPError, ValueError):
                pass  # API restarting; try again next round
            await asyncio.sleep(poll_s)

    async def _notify(self, job: dict[str, Any]) -> None:
        jid = job["id"]
        if not self.delivered.seen(f"start:{jid}"):
            await self._broadcast(notice_started(job))
            self.delivered.add(f"start:{jid}")
        if job["status"] in TERMINAL and not self.delivered.seen(f"end:{jid}"):
            await self._broadcast(notice_finished(job))
            media = media_path(job)
            if media:
                await self._broadcast_file(media)
            self.delivered.add(f"end:{jid}")

    async def _broadcast(self, markdown: str) -> None:
        for chat in self.allowed:
            for part in split_message(markdown, limit=3500):
                await self._send(chat, part)

    async def _broadcast_file(self, path: str) -> None:
        from aiogram.types import FSInputFile

        suffix = Path(path).suffix.lower()
        for chat in self.allowed:
            if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                await self.bot.send_photo(chat, FSInputFile(path))
            elif suffix in {".mp4", ".mov"}:
                await self.bot.send_video(chat, FSInputFile(path))
            else:
                await self.bot.send_audio(chat, FSInputFile(path))

    async def run(self) -> None:
        from aiogram.types import BotCommand

        await self.bot.set_my_commands([BotCommand(command=c, description=d) for c, d, _ in COMMANDS])
        await self.resume()
        if self.notify_sources:
            watcher = asyncio.create_task(self.watch_other_jobs())
            self._tasks.add(watcher)
            watcher.add_done_callback(self._tasks.discard)
        await self.dp.start_polling(self.bot)


TERMINAL = {"done", "failed", "cancelled", "dead"}
LIVE = {"queued", "running", "waiting"}

# (command, menu description, help example) — one table drives the "/" menu, /help and the handlers above.
COMMANDS: list[tuple[str, str, str]] = [
    ("help", "Everything Tony can do", "/help"),
    ("ask", "Ask anything (same as just typing)", "/ask Should we lease or buy our office laptops?"),
    ("search", "Web search with sources", "/search latest MAS guidelines on AI risk management"),
    ("code", "Coding agent builds it in ~/SourceCode (start with a folder to work in it)",
     "/code ~/SourceCode/my-app Add a login page"),
    ("agent", "Run a specific role", "/agent architect Design a payments API for a small bank"),
    ("roles", "List the roles for /agent", "/roles"),
    ("image", "Generate an image", "/image A minimalist poster of a lighthouse at dawn"),
    ("edit", "Edit a photo: send a photo with this caption", "(photo) /edit make the sky stormy"),
    ("video", "Make a video: duration, theme, style", "/video 8s, cinematic drone shot over Kuala Lumpur at sunset"),
    ("film", "A short film from a theme or 'script:' (several shots + score)",
     "/film 30s, a lone astronaut finds a garden on Mars, cinematic"),
    ("animate", "Animate a photo: send a photo with this caption", "(photo) /animate slow push-in, leaves moving"),
    ("music", "Make a song: theme, genre/vibe, duration; Tony writes lyrics unless you add a 'lyrics:' line",
     "/music 90s upbeat Malay pop about rainy KL nights, 2 min"),
    ("say", "Reply with a voice message", "/say Good morning, here is today's plan"),
    ("jobs", "Recent jobs and their status", "/jobs"),
    ("cancel", "Stop a job (newest if no id)", "/cancel"),
]

HELP_TEXT = (
    "**Tony — your local AI** (runs on your Mac; nothing leaves it)\n\n"
    "Just type to ask anything. Every answer is drafted, checked by an independent reviewer and revised "
    "if needed.\n"
    "**Voice note** → transcribed and answered. **Photo** → described or answered about "
    "(add a question as caption).\n\n"
    + "\n".join(f"/{c} — {d}\n  e.g. `{ex}`" for c, d, ex in COMMANDS if c != "help")
    + "\n\n**Music**: say the genre/vibe, duration (up to 4 min) and theme; add your own lyrics after a line "
    "`lyrics:` or Tony writes them.\n"
    "**Video**: say the duration (2-10 s), subject and style; or send a photo with /animate."
)


def roles_text() -> str:
    from law import roles

    loaded = roles.load(LawPaths.from_env().settings / "roles.yaml")
    return "**Roles for /agent**\n" + "\n".join(f"`{n}` — {r.summary}" for n, r in loaded.roles.items())
TELEGRAM_LIMIT = 4000


def queued_ahead(jobs: list[dict[str, Any]], job_id: str) -> int:
    """How many top-level jobs submitted before `job_id` are still queued, running or waiting (newest first)."""
    ids = [j["id"] for j in jobs]
    older = jobs[ids.index(job_id) + 1:] if job_id in ids else []
    return sum(1 for j in older if j.get("parent") is None and j.get("status") in {"queued", "running", "waiting"})


def split_dir(args: str) -> dict[str, str]:
    """`/code ~/SourceCode/app Add login` works in that folder (a new folder is created when its parent exists, e.g.
    `/code ~/SourceCode/new-app ...`); otherwise the translator names a new project folder."""
    first, _, rest = args.strip().partition(" ")
    path = Path(first).expanduser()
    if first.startswith(("~/", "/")) and rest.strip() and (path.is_dir() or path.parent.is_dir()):
        return {"dir": str(path), "prompt": rest.strip()}
    return {"prompt": args.strip()}


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split long answers on paragraph or line boundaries so Telegram's message limit never truncates them."""
    parts: list[str] = []
    rest = text.strip() or "done"
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    parts.append(rest)
    return parts


SOURCE_LABEL = {"cli": "the law command", "api": "the API", "console": "the LAW Console"}


def _what(job: dict[str, Any]) -> str:
    p = job.get("payload") or {}
    return str(p.get("prompt") or p.get("caption") or p.get("text") or p.get("query") or p.get("audio") or "")


def notice_started(job: dict[str, Any]) -> str:
    who = SOURCE_LABEL.get(str(job.get("source")), str(job.get("source")))
    role = (job.get("payload") or {}).get("role")
    head = f"🆕 **Started** `{job['id']}` · {job['kind']}{f' ({role})' if role else ''} · from {who}"
    what = _what(job)
    # The whole prompt (long ones are split across messages by _broadcast); capped only to avoid floods.
    return head + ("\n\n" + (what[:6000] + ("…" if len(what) > 6000 else "")) if what else "")


def outcome_line(job: dict[str, Any]) -> str:
    """The status line for a job that did not finish: cancelled is not a failure."""
    if job.get("status") == "cancelled":
        return "🚫 cancelled"
    return f"❌ failed: {job.get('error') or 'unknown error'}"


def notice_finished(job: dict[str, Any]) -> str:
    result = job.get("result") or {}
    if job["status"] != "done":
        icon = "🚫" if job["status"] == "cancelled" else "❌"
        head = f"{icon} **{job['status'].capitalize()}** `{job['id']}` · {job['kind']}"
        return f"{head}\n\n{job.get('error') or ''}"[:3500]
    head = f"✅ **Done** `{job['id']}` · {job['kind']}"
    review = result.get("review") or result.get("verdict")
    if isinstance(review, dict) and review.get("score") is not None:
        head += f" · reviewer {review['score']}/5" + (" (fixed after review)" if result.get("fixed") else "")
    if result.get("dir"):
        head += f"\nProject: `{result['dir']}`"
    body = str(result.get("answer") or result.get("text") or "")
    return head + ("\n\n" + body[:3000] + ("…" if len(body) > 3000 else "") if body else "")


def media_path(job: dict[str, Any]) -> str | None:
    result = job.get("result") or {}
    for key in ("image", "video", "audio"):
        value = result.get(key)
        if value and Path(str(value)).is_file():
            return str(value)
    return None


class Delivered:
    """Job ids whose reply was sent, persisted so a restart never sends a reply twice."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.ids: set[str] = set(path.read_text().split()) if path.exists() else set()

    def seen(self, job_id: str) -> bool:
        return job_id in self.ids

    def add(self, job_id: str) -> None:
        self.ids.add(job_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keep = sorted(self.ids)[-2000:]
        self.path.write_text("\n".join(keep) + "\n")


def main(paths: LawPaths | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    paths = paths or LawPaths.from_env()
    reg = registry.load(paths.registry_file)
    values = secrets.read(paths.env_file)
    token = values.get("LAW_TELEGRAM_TOKEN")
    if not token:
        log.error("no LAW_TELEGRAM_TOKEN in %s; not starting", paths.env_file)
        return
    allowed = secrets.allowed_telegram_ids(values)
    if not allowed:
        log.error("no LAW_TELEGRAM_USER_IDS set; refusing to start an open bot")
        return
    bot = LawBot(token, allowed, f"http://127.0.0.1:{reg.ports.law_api}",
                 notify_sources=set(reg.telegram.notify_sources))
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
