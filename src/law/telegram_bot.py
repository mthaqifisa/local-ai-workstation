"""Telegram bot: the primary channel. Thin adapter over the LAW job API.

Internet-reachable, so it answers only the allow-listed numeric user IDs in ~/LAW/state/.env
(LAW_TELEGRAM_USER_IDS). This is an allow-list, not a login.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from law import board, bugs, projects, registry, secrets
from law.paths import LawPaths
from law.project_memory import ProjectMemory
from law.tg_format import to_telegram_html

log = logging.getLogger("law.telegram")


class LawBot:
    def __init__(self, token: str, allowed: set[int], api_base: str, delivered: Path | None = None,
                 notify_sources: set[str] | None = None, history: Path | None = None) -> None:
        self.bot = Bot(token)
        self.dp = Dispatcher()
        self.allowed = allowed
        self.api = api_base.rstrip("/")
        self.delivered = Delivered(delivered or LawPaths.from_env().state / "telegram-delivered.txt")
        self.history = ChatHistory(history or LawPaths.from_env().state / "telegram-chats")
        self._tasks: set[asyncio.Task[None]] = set()
        self.notify_sources = notify_sources or set()
        self.pending: dict[int, tuple[str, str]] = {}  # chat -> (project, action) waiting for the owner's note
        self.following: set[str] = set()  # jobs whose progress this process is streaming to a chat
        self._wire()

    def _ok(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in self.allowed

    def _wire(self) -> None:
        dp = self.dp

        @dp.message(Command("start", "help"))
        async def help_(message: Message) -> None:
            if self._ok(message):
                await self._send(message.chat.id, HELP_TEXT)

        @dp.message(Command("new"))
        async def new(message: Message) -> None:
            if self._ok(message):
                self.history.clear(message.chat.id)
                await self._send(message.chat.id, "🆕 New conversation: Tony has forgotten the earlier messages.")

        @dp.message(Command("ask"))
        async def ask(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "ask", {"prompt": command.args, "role": "assistant"})

        @dp.message(Command("code"))
        async def code(message: Message, command: CommandObject) -> None:
            if not self._ok(message) or not command.args:
                return
            args = command.args.strip()
            if args.lower() == "init" or re.match(r"(?i)new\s*\n", args):  # a template, not "/code newsletter ..."
                await self._start_project(message, args, "code")
                return
            await self._job(message, "task", {"role": "coder", **split_dir(args)})

        @dp.message(Command("project"))
        async def project(message: Message, command: CommandObject) -> None:
            if self._ok(message):
                await self._start_project(message, (command.args or "init").strip(), "project")

        for name in ("go", "nogo", "changes", "allgood", "resume", "push", "email"):
            self._wire_decision(name)

        @dp.message(Command("note"))
        async def note(message: Message, command: CommandObject) -> None:
            if not self._ok(message):
                return
            pid, _, text = (command.args or "").strip().partition(" ")
            if not pid or not text.strip():
                await self._send(message.chat.id, "Usage: `/note <project> <what the agent should know>`")
                return
            try:
                project = projects.Projects(LawPaths.from_env()).get(pid)
            except KeyError as exc:
                await self._send(message.chat.id, str(exc))
                return
            ProjectMemory(Path(project.dir)).add_note(text)
            board.add_note(LawPaths.from_env(), project.id, text, author="owner (Telegram)")  # the ticket in progress
            await self._send(message.chat.id, f"📨 Noted for **{project.name}**: the agent gets it at its next action.")

        @dp.message(Command("bugfix"))
        async def bugfix(message: Message, command: CommandObject) -> None:
            if not self._ok(message):
                return
            pid, _, text = (command.args or "").strip().partition(" ")
            if not pid or not text.strip():
                await self._send(message.chat.id, "Usage: `/bugfix <project> <what goes wrong, and what you expected>`")
                return
            try:
                project = projects.Projects(LawPaths.from_env()).get(pid)
            except KeyError as exc:
                await self._send(message.chat.id, str(exc))
                return
            title, _, details = text.strip().partition("\n")
            bug = bugs.add(Path(project.dir), title, details, reporter="owner")
            if await self._project_busy(project.id):  # a build is running: the agent at work takes it now
                ProjectMemory(Path(project.dir)).add_note(
                    f"The owner reported {bug.id} (in .law/BUGS.md): {bug.title}. Finish your current step, then fix "
                    f"it with a test, set its status to fixed, and carry on. QA retests it in the next QA round.")
                await self._send(message.chat.id, f"🐞 **{bug.id}** logged for **{project.name}**. A build is running: "
                                                  "the agent fixes it after its current step, and QA retests it.")
                return
            await self._send(message.chat.id, f"🐞 **{bug.id}** logged for **{project.name}**: fixing it now; QA "
                                              "retests the product after the fix.")
            await self._submit(message.chat.id, "project_bugfix", {"project": project.id})

        @dp.message(Command("projects"))
        async def projects_(message: Message) -> None:
            if self._ok(message):
                rows = projects.Projects(LawPaths.from_env()).all()
                lines = [f"`{p.id}` v{p.version}: {p.state.replace('_', ' ')}" for p in rows]
                await self._send(message.chat.id, "**Projects**\n" + ("\n".join(lines) or "none yet: `/code init`"))

        @dp.callback_query(F.data.startswith("j|"))
        async def job_button(query: CallbackQuery) -> None:
            if query.from_user.id not in self.allowed or not query.data or not isinstance(query.message, Message):
                return
            _, action, job_id = query.data.split("|", 2)
            await query.answer()
            await query.message.edit_reply_markup(reply_markup=None)
            await self._job_action(query.message.chat.id, job_id, action)

        @dp.message(Command("hardreset"))
        async def hardreset(message: Message) -> None:
            if not self._ok(message):
                return
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🧨 Reset now", callback_data="r|yes"),
                InlineKeyboardButton(text="Keep everything", callback_data="r|no")]])
            await self.bot.send_message(message.chat.id, to_telegram_html(HARD_RESET_TEXT), parse_mode="HTML",
                                        reply_markup=markup)

        @dp.callback_query(F.data.startswith("r|"))
        async def reset_button(query: CallbackQuery) -> None:
            if query.from_user.id not in self.allowed or not query.data or not isinstance(query.message, Message):
                return
            await query.answer()
            await query.message.edit_reply_markup(reply_markup=None)  # one answer per question
            chat = query.message.chat.id
            if query.data != "r|yes":
                await self._send(chat, "Nothing was reset.")
                return
            status = await self.bot.send_message(chat, "🧨 resetting: stopping jobs, then clearing (a few minutes)…")
            reply = {"chat_id": chat, "status_id": status.message_id}
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=10)) as client:
                    r = await client.post(f"{self.api}/reset")
                    r.raise_for_status()
                    done = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                await self._edit(reply, f"reset failed: {exc}")
                return
            self.pending.pop(chat, None)
            await self._edit(reply, reset_summary(done), rich=True)

        @dp.callback_query(F.data.startswith("p|"))
        async def decision_button(query: CallbackQuery) -> None:
            if query.from_user.id not in self.allowed or not query.data or not isinstance(query.message, Message):
                return
            _, action, pid = query.data.split("|", 2)
            await query.answer()
            chat = query.message.chat.id
            await query.message.edit_reply_markup(reply_markup=None)  # one decision per question
            await self._decide(chat, pid, action, "")

        @dp.message(Command("agent"))
        async def agent(message: Message, command: CommandObject) -> None:
            if not self._ok(message):
                return
            parts = (command.args or "").split(maxsplit=1)
            if len(parts) < 2:
                await self._send(message.chat.id, "Usage: `/agent <role> <task>` — see /roles")
                return
            await self._job(message, "task", {"role": parts[0], **split_dir(parts[1])})

        @dp.message(Command("proposal"))
        async def proposal(message: Message, command: CommandObject) -> None:
            if self._ok(message) and command.args:
                await self._job(message, "proposal", split_dir(command.args), file_field="pdf")

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

        @dp.message(Command("jobs", "job"))  # /job: the singular is a common slip
        async def jobs(message: Message, command: CommandObject) -> None:
            """`/jobs`: what is running (and what failed today) with health; `/jobs <keyword>`: details + buttons."""
            if not self._ok(message):
                return
            query = (command.args or "").strip()
            async with httpx.AsyncClient(timeout=20) as client:
                data = (await client.get(f"{self.api}/agents", params={"q": query})).json()
            found = data.get("jobs", [])
            if not query:
                lines = [job_line(j) for j in found] or ["Nothing running, and nothing failed today."]
                await self._send(message.chat.id, "**Jobs**\n" + "\n".join(lines) +
                                 "\n\nDetails and buttons: `/jobs <project or keyword>`")
                return
            if not found:
                await self._send(message.chat.id, f"No running or recent job matches `{query}`.")
                return
            for j in found[:3]:
                await self._ask(message.chat.id, job_card(j, data.get("model") or {}),
                                {"job": j["id"], "actions": j.get("actions", [])})

        @dp.message(Command("retry"))
        async def retry(message: Message, command: CommandObject) -> None:
            if not self._ok(message):
                return
            job_id = (command.args or "").strip()
            if not job_id:
                await self._send(message.chat.id, "Usage: `/retry <job id>` (see /jobs)")
                return
            await self._job_action(message.chat.id, job_id, "retry")

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
            if message.chat.id in self.pending and not message.text.startswith("/"):
                pid, action = self.pending.pop(message.chat.id)
                note = "" if message.text.strip().lower() == "stop" else message.text.strip()
                await self._submit(message.chat.id, "project_step",
                                   {"project": pid, "action": action, "note": note, "chat": message.chat.id})
                return
            if message.text.startswith("/"):
                await self._send(message.chat.id, "Unknown command. Send /help for the list.")
                return
            await self._job(message, "ask", {"prompt": message.text, "role": "assistant"})

    async def _job_action(self, chat: int, job_id: str, action: str) -> None:
        """Retry, skip, restart or cancel a job from a button, then follow it again in this chat."""
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{self.api}/jobs/{job_id}/{action}")
        if r.status_code != 200:
            is_json = r.headers.get("content-type", "").startswith("application/json")
            detail = r.json().get("detail", r.text) if is_json else r.text
            await self._send(chat, f"Couldn't {action} `{job_id}`: {detail}")
            return
        if action == "cancel":
            await self._send(chat, f"Cancelling `{job_id}`.")
            return
        follow = r.json().get("parent") or r.json()["id"]  # a pipeline continues under its root
        label = {"retry": "🔁 retrying", "skip": "⏭ skipped the failed step; continuing",
                 "restart": "🔄 restarting the stuck step"}[action]
        status = await self.bot.send_message(chat, f"{label} `{follow}`")
        reply = {"chat_id": chat, "status_id": status.message_id, "file_field": None, "then": None}
        await self._deliver(follow, reply, again=True)

    async def _start_project(self, message: Message, args: str, mode: str) -> None:
        """`init` sends the template for this mode; a filled template (`new ...`) starts the project."""
        if args.lower() == "init":
            await self._send(message.chat.id, projects.INTROS[mode])
            await self.bot.send_message(message.chat.id, f"<pre>{projects.TEMPLATES[mode]}</pre>", parse_mode="HTML")
            return
        try:
            fields = projects.parse_template(args)
        except projects.TemplateError as exc:
            await self._send(message.chat.id, f"Not started: {exc}. Send `/{mode} init` for the template.")
            return
        await self._submit(message.chat.id, "project_new", {"fields": fields, "chat": message.chat.id, "mode": mode})

    def _wire_decision(self, action: str) -> None:
        """/go <project>, /nogo <project> [what to change], /changes <project> <what>, /allgood, /resume, /push."""

        @self.dp.message(Command(action))
        async def handler(message: Message, command: CommandObject) -> None:
            if not self._ok(message):
                return
            pid, _, note = (command.args or "").strip().partition(" ")
            if not pid:
                await self._send(message.chat.id, f"Usage: `/{action} <project>` (see /projects)")
                return
            await self._decide(message.chat.id, pid, action, note.strip())

    async def _decide(self, chat: int, pid: str, action: str, note: str) -> None:
        """Run the decision, or first ask for the note it needs."""
        if action in NEEDS_NOTE and not note:
            self.pending[chat] = (pid, action)
            extra = " Or send `stop` to park the project." if action == "nogo" else ""
            await self._send(chat, f"What should change in **{pid}**? Reply with your notes.{extra}")
            return
        await self._submit(chat, "project_step", {"project": pid, "action": action, "note": note, "chat": chat})

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
        await self._submit(message.chat.id, kind, payload, file_field=file_field, then=then)

    async def _submit(self, chat: int, kind: str, payload: dict[str, Any], *,
                      file_field: str | None = None, then: str | None = None) -> None:
        """Submit a job that remembers where to reply, then deliver the result (resumable after restarts).
        A chat question carries the recent conversation, so "continue" or "yes" means something."""
        if kind == "ask":
            payload = {**payload, "history": self.history.get(chat)}
        status = await self.bot.send_message(chat, "⏳ queued")
        reply = {"chat_id": chat, "status_id": status.message_id, "file_field": file_field, "then": then}
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

    async def _project_busy(self, pid: str) -> bool:
        """A pipeline of this project is queued or running."""
        async with httpx.AsyncClient(timeout=30) as client:
            data = (await client.get(f"{self.api}/jobs", params={"limit": 200})).json()
        return any(j.get("parent") is None and j.get("status") in {"queued", "running", "waiting"}
                   and (j.get("payload") or {}).get("project") == pid for j in data)

    async def _ahead_of(self, job_id: str) -> int:
        """Top-level jobs already running or waiting that this one queues behind."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                jobs = (await client.get(f"{self.api}/jobs", params={"limit": 100})).json()
        except (httpx.HTTPError, ValueError):
            return 0
        return queued_ahead(jobs, job_id)

    async def _deliver(self, job_id: str, reply: dict[str, Any], *, again: bool = False) -> None:
        """Stream progress, then send the result. `again` delivers a retried job whose first outcome was sent."""
        if (self.delivered.seen(job_id) and not again) or job_id in self.following:
            return
        self.following.add(job_id)
        try:
            # following a retried job again: its history is old news, show only what happens from now on
            await self._follow(job_id, reply, since=time.time() - 5 if again else 0.0)
        finally:
            self.following.discard(job_id)

    async def _follow(self, job_id: str, reply: dict[str, Any], since: float = 0.0) -> None:
        last = ""
        while True:  # stream progress; reconnect and poll if the API restarts
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as client, \
                        client.stream("GET", f"{self.api}/jobs/{job_id}/stream") as stream:
                    async for line in stream.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        ev = json.loads(line[6:])
                        if float(ev.get("t", 0)) < since:
                            continue
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
        self.history.remember(chat, final)
        if final.get("status") != "done":
            await self._edit(reply, outcome_line(final))
            await self._offer_recovery(chat, final)
            return
        if reply.get("then") == "ask" and result.get("text"):
            await self._edit(reply, f"heard: {result['text'][:300]}")
            async with httpx.AsyncClient(timeout=30) as client:
                status = await self.bot.send_message(chat, "working…")
                follow = {"chat_id": chat, "status_id": status.message_id, "file_field": None, "then": None}
                r = await client.post(f"{self.api}/jobs", json={
                    "kind": "ask", "payload": {"prompt": result["text"], "role": "assistant",
                                               "history": self.history.get(chat)},
                    "source": "telegram", "reply_to": follow})
            if r.status_code == 201:
                await self._deliver(r.json()["id"], follow)
            return
        if result.get("gate"):  # a project question: the PDF (if any), then the question with its buttons
            await self._edit(reply, "done")
            if result.get("pdf"):
                try:
                    await self.bot.send_document(chat, FSInputFile(str(result["pdf"])))
                except Exception:  # the email already has it; the question matters more
                    log.warning("could not attach %s", result["pdf"])
            await self._ask(chat, str(result.get("answer", "")), result["gate"])
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
            elif field == "pdf":
                await self.bot.send_document(chat, path, caption=f"📧 also emailed to {result.get('emailed', 'you')}")
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

    async def _offer_recovery(self, chat: int, job: dict[str, Any]) -> None:
        """Every failed or cancelled job asks the owner what to do: Retry (and Skip for multi-step jobs)."""
        pipeline = bool((job.get("payload") or {}).get("_stages"))
        actions = ["retry", "skip"] if pipeline else ["retry"]
        text = (f"`{job['id']}` ({job.get('kind')}) stopped. " +
                ("**Retry** runs the failed step again, keeping earlier work; **Skip** carries on without it."
                 if pipeline else "**Retry** runs it again."))
        await self._ask(chat, text, {"job": job["id"], "actions": actions})

    async def _ask(self, chat: int, markdown: str, gate: dict[str, Any]) -> None:
        """A message with buttons: a project decision (`project`) or a job action (`job`: retry, skip, ...)."""
        if "job" in gate:
            buttons = [InlineKeyboardButton(text=JOB_BUTTONS[a], callback_data=f"j|{a}|{gate['job']}")
                       for a in gate.get("actions", []) if a in JOB_BUTTONS]
        else:
            buttons = [InlineKeyboardButton(text=BUTTONS[a], callback_data=f"p|{a}|{gate['project']}")
                       for a in gate.get("actions", []) if a in BUTTONS]
        markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
        try:
            await self.bot.send_message(chat, to_telegram_html(markdown), parse_mode="HTML",
                                        disable_web_page_preview=True, reply_markup=markup)
        except Exception:  # bad markup: plain text, keep the buttons
            await self.bot.send_message(chat, markdown, reply_markup=markup)

    async def _send(self, chat: int, markdown: str) -> None:
        try:
            await self.bot.send_message(chat, to_telegram_html(markdown), parse_mode="HTML",
                                        disable_web_page_preview=True)
        except Exception:  # Telegram rejected the markup: never lose the answer, send it plain
            await self.bot.send_message(chat, markdown)

    async def _edit(self, reply: dict[str, Any], text: str, *, rich: bool = False) -> None:
        """Update the job's status message in place. Without one (a job submitted elsewhere or resumed after a
        restart) or when it can no longer be edited, send a new one and keep editing that, so progress never
        floods the chat with a message per step."""
        chat, msg = reply["chat_id"], reply.get("status_id")
        body = to_telegram_html(text) if rich else text
        mode = "HTML" if rich else None
        if msg:
            try:
                # progress lines name the pages agents read: never let Telegram turn them into link previews
                await self.bot.edit_message_text(body, chat_id=chat, message_id=msg, parse_mode=mode,
                                                 disable_web_page_preview=True)
                return
            except Exception as exc:
                if "not modified" in str(exc).lower():  # the same text again: nothing to do
                    return
                if rich:  # maybe the markup: try plain text in the same message before giving up on it
                    try:
                        await self.bot.edit_message_text(text, chat_id=chat, message_id=msg,
                                                         disable_web_page_preview=True)
                        return
                    except Exception as plain_exc:
                        if "not modified" in str(plain_exc).lower():
                            return
        try:
            sent = await self.bot.send_message(chat, body, parse_mode=mode, disable_web_page_preview=True)
        except Exception:
            sent = await self.bot.send_message(chat, text, disable_web_page_preview=True)
        reply["status_id"] = sent.message_id  # later updates edit this message

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
                # resumed after a restart: show progress from now on, not the job's whole history again
                task = asyncio.create_task(self._deliver(job["id"], dict(job["reply_to"]), again=True))
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
                    await self._reattach(job)
                    if (job.get("parent") is None and job.get("source") in self.notify_sources
                            and (job["created"] >= started_at or job["status"] in LIVE)):
                        await self._notify(job)
            except (httpx.HTTPError, ValueError):
                pass  # API restarting; try again next round
            await asyncio.sleep(poll_s)

    async def _reattach(self, job: dict[str, Any]) -> None:
        """A Telegram job reopened elsewhere (Retry/Skip/Restart in the Console) is followed in its chat again."""
        runs = int((job.get("payload") or {}).get("_runs", 0))
        key = f"run{runs}:{job['id']}"
        # also jobs for a Telegram chat that were submitted elsewhere (API, Console), once old enough that this
        # process would already be following them if it had submitted them itself
        external = not runs and time.time() - float(job.get("created", 0)) > 10 and not self.delivered.seen(job["id"])
        if ((runs or external) and job.get("source") == "telegram" and job.get("parent") is None
                and job["status"] in LIVE and job.get("reply_to") and job["id"] not in self.following
                and not self.delivered.seen(key)):
            self.delivered.add(key)
            chat = job["reply_to"]["chat_id"]
            note = "🔁 resumed" if runs else "▶️ started"  # started: e.g. an agent Tony handed the work to
            status = await self.bot.send_message(chat, f"`{job['id']}` {note}; following it here")
            reply = {**job["reply_to"], "status_id": status.message_id}
            task = asyncio.create_task(self._deliver(job["id"], reply, again=True))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _notify(self, job: dict[str, Any]) -> None:
        jid = job["id"]
        if not self.delivered.seen(f"start:{jid}"):
            await self._broadcast(notice_started(job))
            self.delivered.add(f"start:{jid}")
        if job["status"] in TERMINAL and not self.delivered.seen(f"end:{jid}"):
            await self._broadcast(notice_finished(job))
            if job["status"] in {"failed", "dead", "cancelled"}:
                for chat in self.allowed:
                    await self._offer_recovery(chat, job)
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
            elif suffix == ".pdf":
                await self.bot.send_document(chat, FSInputFile(path))
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
BUTTONS = {"go": "✅ Go", "nogo": "❌ No go", "allgood": "👍 All good", "changes": "🛠 Needs changes",
           "push": "⬆️ Push", "resume": "▶️ Resume", "email": "📧 Email it"}
NEEDS_NOTE = {"nogo", "changes", "resume"}
JOB_BUTTONS = {"retry": "🔁 Retry", "skip": "⏭ Skip", "restart": "🔄 Restart step", "cancel": "🛑 Cancel"}
HEALTH = {"working": "🟢 working", "thinking": "🔵 thinking", "queued": "🔵 queued", "stalled": "🟠 stalled",
          "stuck": "🟠 stuck", "failed": "🔴 failed", "cancelled": "🔴 cancelled", "done": "✅ done"}


def _ago(t: float | None) -> str:
    return "" if not t else f"{max(0, round((time.time() - t) / 60))} min ago"


def job_line(j: dict[str, Any]) -> str:
    s = j.get("stage")
    where = f" · step {s['index']}/{s['total']} {s.get('role') or ''} {s['kind']}" if s else ""
    last = j.get("last_action")
    tail = f" · {last['message'][:60]} ({_ago(last['t'])})" if last and j["health"] in {"working", "thinking",
                                                                                          "stalled"} else ""
    return f"{HEALTH.get(j['health'], j['health'])} `{j['id']}` **{j['title'][:40]}**{where}{tail}"


def job_card(j: dict[str, Any], model: dict[str, Any]) -> str:
    """A job's full status for Telegram: health, step, last action, to-do list, model activity."""
    lines = [f"{HEALTH.get(j['health'], j['health'])} **{j['title']}** `{j['id']}`"]
    s = j.get("stage")
    if s:
        lines.append(f"Step {s['index']}/{s['total']}: {s.get('role') or ''} {s['kind']} ({s['status']}"
                     + (f", {_ago(s['started']).replace(' ago', '')}" if s.get("started") else "") + ")")
    last = j.get("last_action")
    if last:
        lines.append(f"Last action: {last['message'][:120]} ({_ago(last['t'])})")
    c = j.get("last_10min") or {}
    lines.append(f"Last 10 min: {c.get('reads', 0)} reads · {c.get('commands', 0)} commands · "
                 f"{c.get('edits', 0)} edits · {c.get('web', 0)} web")
    if j.get("last_write"):
        lines.append(f"Last file change: {_ago(j['last_write'])}")
    p = j.get("plan")
    if p and p.get("total"):
        lines.append(f"To-do: {p['done']}/{p['total']} done" + (f" · next: {p['next'][:80]}" if p.get("next") else ""))
    if j["health"] in {"working", "thinking", "stalled"}:
        last_step = model.get("last") or {}
        lines.append("Model: " + ("generating now" if model.get("active") else "idle")
                     + (f" · last step {last_step['tok_s']} tok/s, context {round(last_step['context'] / 1000)}K"
                        if last_step else ""))
    if j.get("error"):
        lines.append(f"Error: {j.get('error_line') or j['error'][:240]}")
    if j.get("project"):
        lines.append(f"Folder: `{j['project']}`")
    return "\n".join(lines)
LIVE = {"queued", "running", "waiting"}

# (command, menu description, help example) — one table drives the "/" menu, /help and the handlers above.
COMMANDS: list[tuple[str, str, str]] = [
    ("help", "Everything Tony can do", "/help"),
    ("new", "Start a new conversation (Tony forgets the earlier messages)", "/new"),
    ("hardreset", "Stop all jobs and clear all history, chat memory and caches (asks first; keeps models, "
     "projects, outputs and settings)", "/hardreset"),
    ("ask", "Ask anything (same as just typing)", "/ask Should we lease or buy our office laptops?"),
    ("search", "Web search with sources", "/search latest MAS guidelines on AI risk management"),
    ("project", "Full project (SDLC): template → research and proposal PDF (emailed) → your go → build and test → "
     "handover document → your verification → enhancements", "/project init"),
    ("code", "Code only: template → a short plan in the chat → your go → build and test → your verification. "
     "Or `/code [folder] <task>` for a quick task", "/code init"),
    ("projects", "Your projects and where each one is (go/no go, building, verification)", "/projects"),
    ("go", "Approve a proposal and start development", "/go office-chat"),
    ("nogo", "Reject a proposal: say what to change (revised proposal), or `stop` to park it",
     "/nogo office-chat make it mobile-first"),
    ("changes", "After you try it: what to change or add (enhanced proposal, then build)",
     "/changes office-chat add file sharing"),
    ("allgood", "Accept the build: the project is done", "/allgood office-chat"),
    ("email", "Email a project's latest document (proposal or handover) again", "/email office-chat"),
    ("note", "Tell the agent building a project something while it works", "/note office-chat use the PEM files"),
    ("bugfix", "Report a bug: the agent fixes it and QA retests the product",
     "/bugfix office-chat the send button does nothing"),
    ("proposal", "Consultant-grade proposal: research, full document (design, architecture, flows, "
     "wireframes, plan, cost), reviewed, PDF emailed to you; start with a project folder to use it as the prototype",
     "/proposal ~/SourceCode/my-app Propose this as a product for small offices"),
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
    ("jobs", "What's running and its health (working / thinking / stalled / failed); add a project or keyword for "
     "details with Retry / Skip / Restart buttons", "/jobs lan-chat"),
    ("cancel", "Stop a job (newest if no id)", "/cancel"),
    ("retry", "Run a failed job again from the step that failed (earlier work is kept)", "/retry 8f22ebbaef49"),
]

HELP_TEXT = (
    "**Tony — your local AI** (runs on your Mac; nothing leaves it)\n\n"
    "Just type to ask anything; Tony remembers the recent conversation (/new starts over). Short replies come "
    "straight back; longer answers are checked by an independent reviewer and revised if needed. Work that needs "
    "files, code, documents or apps (\"study this project and email me a PDF report\") goes to an agent, which "
    "reports progress and the result here.\n"
    "**Voice note** → transcribed and answered. **Photo** → described or answered about "
    "(add a question as caption).\n\n"
    + "\n".join(f"/{c} — {d}\n  e.g. `{ex}`" for c, d, ex in COMMANDS if c != "help")
    + "\n\n**Projects**: `/project init` (full SDLC) or `/code init` (code only) sends a template; fill it in "
    "and send it back. A project researches and emails a proposal; a code-only one posts a short plan here. Then "
    "**Go / No go**: on Go, Tony prepares the tools, builds in milestones, tests and fixes until an independent "
    "review passes (a project also writes a handover document with screenshots), then asks you to verify: "
    "**All good** finishes it, **Needs changes** brings a revised proposal or plan and the loop repeats.\n"
    "\n**Music**: say the genre/vibe, duration (up to 4 min) and theme; add your own lyrics after a line "
    "`lyrics:` or Tony writes them.\n"
    "**Video**: say the duration (2-10 s), subject and style; or send a photo with /animate."
)


HARD_RESET_TEXT = (
    "**Hard reset?**\n"
    "Stops every running or queued job, then deletes:\n"
    "• all job history and progress logs\n"
    "• agents' saved conversations\n"
    "• Tony's chat memory (all chats)\n"
    "• every LAW cache: model prompt cache (the model server restarts), Python/npm/Maven packages, temp files, "
    "unused Docker images\n\n"
    "**Kept:** models, settings, secrets, your projects and their plans, generated files (images, videos, "
    "documents), and the jobs of unfinished projects so **Retry** still continues their build."
)


def reset_summary(done: dict[str, Any]) -> str:
    gb = int(done.get("freed_bytes", 0)) / 1024 ** 3
    lines = [
        "✅ **Hard reset done**",
        f"• {done.get('cancelled', 0)} job(s) stopped, {done.get('jobs_deleted', 0)} deleted from history",
        f"• {done.get('agent_sessions_deleted', 0)} agent conversation(s) and {done.get('chats_cleared', 0)} chat "
        "memory file(s) cleared",
        f"• caches cleared: {gb:.1f} GB freed",
    ]
    if done.get("project_jobs_kept"):
        lines.append(f"• kept {done['project_jobs_kept']} job(s) of unfinished projects (Retry continues them)")
    if done.get("skipped_busy"):
        lines.append(f"• not cleared because a job was still stopping: {', '.join(done['skipped_busy'])}; "
                     "run /hardreset again in a minute")
    return "\n".join(lines)


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


class ChatHistory:
    """The recent conversation per chat (question, answer, agent results), kept in ~/LAW/state across restarts."""

    def __init__(self, folder: Path, messages: int = 16, chars: int = 6000) -> None:
        self.folder = folder
        self.messages = messages
        self.chars = chars

    def _file(self, chat: int) -> Path:
        return self.folder / f"{chat}.json"

    def get(self, chat: int) -> list[dict[str, str]]:
        try:
            data = json.loads(self._file(chat).read_text())
        except (OSError, ValueError):
            return []
        return [m for m in data if isinstance(m, dict) and m.get("role") in {"user", "assistant"}] \
            if isinstance(data, list) else []

    def add(self, chat: int, role: str, content: str) -> None:
        text = content.strip()
        if len(text) > self.chars:
            text = text[:self.chars] + " … (cut)"
        if not text:
            return
        items = self.get(chat)
        if items and items[-1]["role"] == role:  # chat templates expect turns to alternate
            items[-1] = {"role": role, "content": f"{items[-1]['content']}\n\n{text}"}
        else:
            items.append({"role": role, "content": text})
        items = items[-self.messages:]
        while items and items[0]["role"] != "user":  # a conversation starts with the user
            items.pop(0)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._file(chat).write_text(json.dumps(items, ensure_ascii=False))

    def remember(self, chat: int, job: dict[str, Any]) -> None:
        """Record a finished job delivered in this chat: a question and its answer, or an agent's outcome."""
        payload = job.get("payload") or {}
        result = job.get("result") or {}
        if job.get("kind") == "ask" and payload.get("prompt"):
            self.add(chat, "user", str(payload["prompt"]))
            answer = result.get("answer") if job.get("status") == "done" else None
            self.add(chat, "assistant", str(answer or f"(no answer: {job.get('status')}, {job.get('error')})"))
            return
        role = payload.get("role") or ""
        outcome = (result.get("answer") or result.get("text") or "") if job.get("status") == "done" \
            else f"{job.get('status')}: {job.get('error')}"
        self.add(chat, "assistant", f"[{job.get('kind')} job {job.get('id')}{f' ({role} agent)' if role else ''} "
                                    f"{job.get('status')}] {outcome}".strip())

    def clear(self, chat: int) -> None:
        self._file(chat).unlink(missing_ok=True)


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
