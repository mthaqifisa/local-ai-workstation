"""Telegram bot — use the workstation from your phone.

Runs as launchd service com.law.telegram, but ONLY when ~/.law/.env has both
TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID (set them with `law configure`). It talks to the
Telegram Bot API with long polling (no open port, no webhook) and puts every request into
the same job queue as everything else, so it takes turns with the CLI, Open WebUI, etc.

Security: agents have full access to the Mac, and anyone on Telegram can message a bot.
So only the user id(s) in TELEGRAM_USER_ID are obeyed; everyone else gets one "this bot
is private" reply and is ignored afterwards.

What you can send
  plain text          → an agent task (default persona `general`) in the chat's project folder
  /ask <question>     → a quick chat answer, no tools
  /plan <task>        → only translate the task into a spec and show it; /go runs that spec
  /raw <task>         → agent task exactly as written (no translator)
  /status  /jobs      → worker, memory, queue / recent jobs
  /cancel [id]        → cancel (default: the last job from this chat)
  /resume [id]        → continue a failed / cancelled / stopped agent job
  /undo               → roll the project back to the snapshot taken before the last agent job
  /persona [name]     → show / change the persona used for plain messages
  /project [folder]   → show / change the project folder (name → ~/MLX-AI/source/<name>)

Progress: every job gets ONE message that is edited every few seconds with the job state
and the latest steps (model loading, tool calls, …); the final answer arrives as new
message(s). Messages are plain text (no Markdown), so nothing ever fails to render.

State (update offset, per-chat settings, jobs being followed) is kept in
~/.law/state/telegram.json, so a restart neither re-runs old messages nor loses track of
running jobs. A heartbeat file (~/.law/run/telegram.beat) lets the watchdog restart a
frozen bot.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import random
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .paths import Paths, get_paths
from .registry import load
from .util import atomic_write, read_env

log = logging.getLogger("law.telegram")

API = "https://api.telegram.org"
MAX_MSG = 4000
# poll_s is the long-poll hold time. api.telegram.org resets the held connection on a flaky link, so a
# SHORTER hold = less reset exposure + the watchdog heartbeat is refreshed twice as often (every loop
# touches it). 25s keeps us well under the 300s stale threshold even through a burst of resets.
DEFAULTS = {"project": "telegram", "persona": "general", "poll_s": 25, "edit_every_s": 3,
            "progress_lines": 8, "stale_s": 300}
HISTORY_TURNS = 12            # messages of per-chat conversation kept for follow-up context
HISTORY_CHARS = 1500         # each stored message is trimmed to this many characters
ICON = {"queued": "⏳", "running": "⚙️", "waiting": "⏸", "done": "✅", "failed": "❌", "cancelled": "⏹"}
VOICES = {"af_sky", "af_heart", "am_adam", "am_echo", "bf_emma", "bm_lewis"}

HELP = """Local AI Workstation — Telegram

Ask a question in plain words and I answer it here (you'll see me think). Give a task in plain words
(build/fix/add/deploy …) and an agent does it on the Mac with tools, in your project folder.
Send a PHOTO (with an optional caption like "where is this?") and I analyse it for you.
Send a VOICE note and I transcribe it, then do exactly what you said.

/ask <question> — force a quick answer, no tools
/image <prompt> — generate an image and send it here
/voice [voice] <text> — generate speech and send the audio
/video <prompt> — generate a short video and send it
/plan <task> — show the task spec first; /go runs it
/raw <task> — run your words exactly as written
/status — worker, memory, queue
/jobs — recent jobs
/cancel [id] — cancel (default: your last job)
/resume [id] — continue a failed or stopped agent job
/remind <in 30m | 14:30> <what> — one-off reminder
/daily <HH:MM> <task> — run a task every day (e.g. /daily 08:00 summarise today's AI news)
/schedules — list reminders & daily tasks · /unschedule <id> — remove one
/undo — roll back the last agent job's changes
/persona [name] — show / change the persona
/project [folder] — show / change the project folder
/help — this message"""


# ── configuration ───────────────────────────────────────────────────────
def creds(paths: Paths) -> tuple[str | None, set[int]]:
    env = read_env(paths.env_file)
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
    users: set[int] = set()
    for part in (env.get("TELEGRAM_USER_ID") or "").replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            users.add(int(part))
    return token, users


def configured(paths: Paths) -> bool:
    token, users = creds(paths)
    return bool(token and users)


def beat_file(paths: Paths) -> Path:
    return paths.run / "telegram.beat"


def beat_age(paths: Paths) -> float | None:
    """Seconds since the bot last polled, or None if it isn't configured / never ran."""
    f = beat_file(paths)
    if not configured(paths) or not f.exists():
        return None
    return max(0.0, time.time() - f.stat().st_mtime)


def mask(token: str | None) -> str:
    if not token:
        return "(not set)"
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "…"


def live_split(raw: str) -> tuple[str, str]:
    """(thinking, answer) for a possibly-incomplete streamed reply. Qwen3 chat templates open
    the thinking block in the prompt, so a reply streams thinking FIRST, then '</think>', then
    the answer. Until '</think>' arrives everything streamed so far is still thinking — which is
    the opposite of split_think() (that one is for a finished reply)."""
    raw = raw or ""
    if "</think>" in raw:
        think, answer = raw.split("</think>", 1)
        return think.replace("<think>", "").strip(), answer.strip()
    return raw.replace("<think>", "").strip(), ""


# ── plain-language media routing ────────────────────────────────────────
# A plain Telegram message like "make me an image of a sunset" should GENERATE and send media,
# not be handed to the general agent (which can't generate mid-run). We only route when the intent
# is unambiguous — a generate-verb close to a media noun — so ordinary tasks still go to the agent.
_GEN = r"(?:generate|create|make|draw|paint|render|design|produce|give me|show me|i want|i'd like|i need)"
_IMG_NOUN = r"(?:image|images|picture|pictures|photo|photos|pic|pics|artwork|illustration|illustrations|logo|logos|wallpaper|drawing|painting|poster|icon)"
_VID_NOUN = r"(?:video|videos|clip|clips|animation|animations|movie)"
_IMG_RE = re.compile(rf"\b{_GEN}\b.{{0,24}}\b{_IMG_NOUN}\b", re.I | re.S)
_VID_RE = re.compile(rf"\b{_GEN}\b.{{0,24}}\b{_VID_NOUN}\b", re.I | re.S)
_SAY_RE = re.compile(r"^\s*(?:please\s+)?(?:can you\s+)?(?:say|speak|narrate|voice)\b[:,]?\s+(.+)", re.I | re.S)
_READ_RE = re.compile(r"\bread\b\s+(?:this\s+|that\s+|the following\s+|it\s+)?(?:aloud|out loud)\b"
                      r"\s*[:,\-]?\s*(.+)", re.I | re.S)


def _subject(text: str) -> str:
    """Pull the described subject out of a generation request (the part after of/showing/…, else
    whatever follows the media noun, else the whole message)."""
    m = re.search(r"\b(?:of|showing|depicting|that shows?|with|about|featuring)\b\s+(.+)", text, re.I | re.S)
    if m:
        return m.group(1).strip().strip('"').strip(" .") or text.strip()
    s = re.sub(rf"^.*?\b(?:{_IMG_NOUN}|{_VID_NOUN})\b[:,\s]*", "", text, flags=re.I | re.S).strip()
    return s or text.strip()


# A plain Telegram message is EITHER a question to answer in chat (quick, streamed, no tools) OR a
# task for an agent to do with tools. We answer clear questions directly so "how do I …?" doesn't
# spin up a whole agent; anything with a build/modify verb stays a task.
_TASK_VERB = re.compile(r"\b(build|create|make|implement|write|code|develop|deploy|install|set ?up|"
                        r"configure|add|remove|delete|fix|debug|refactor|rename|move|update|change|"
                        r"modify|edit|scaffold|clone|download|migrate|optimi[sz]e|rewrite|compile|"
                        r"package|publish|commit|push|benchmark|generate|automate|open|launch|navigate|"
                        r"screenshot|email|play|click|capture|record|upload|research|analyse|analyze)\b",
                        re.I)
_Q_OPEN = re.compile(r"^\s*(?:hey|hi|ok|okay|so|please|um|hmm|yo)?[,\s]*"
                     r"(?:who|what|what's|whats|when|where|why|how|how's|hows|is|are|am|do|does|did|"
                     r"can|could|should|would|will|which|whose|whom|tell me|explain|describe|define|"
                     r"help me understand|any (?:idea|thoughts|way)|is there|are there)\b", re.I)
_POLITE_IMPERATIVE = re.compile(r"^\s*(?:please\s+)?(?:can|could|would|will)\s+you\b", re.I)


# A chat question that depends on CURRENT or external facts (not general knowledge) → look it up on the
# web first. The no-tools chat path can't otherwise answer "what's the weather / latest news / the price".
_LOOKUP_RE = re.compile(
    r"\b(weather|temperature|forecast|rain|news|headline|today|tonight|tomorrow|yesterday|latest|"
    r"current(?:ly)?|right now|this (?:week|month|year)|price|cost|how much (?:is|are|does)|stock|"
    r"exchange rate|score|who won|what happened|release date|when (?:is|was|does|will)|look up|"
    r"search (?:for|the|up)|google|find out|open now|opening hours)\b", re.I)


def needs_lookup(text: str) -> bool:
    return bool(_LOOKUP_RE.search(text or ""))


# A short instruction right after a generation ("make it brighter", "more red", "again") = edit the last
# image/video, not a new task. Re-run the same kind with the tweak folded into the prompt.
_MEDIA_EDIT_RE = re.compile(
    r"^\s*(make it|make them|more |less |add |remove |without |with |change |turn it|now |again\b|"
    r"a bit |bigger|smaller|brighter|darker|lighter|warmer|cooler|longer|shorter|zoom|crop|but |"
    r"instead|different|another (?:angle|version|style|one)|same but)", re.I)


def is_media_edit(text: str) -> bool:
    t = (text or "").strip()
    return len(t) < 120 and bool(_MEDIA_EDIT_RE.match(t))


# A software BUILD is best run by the orchestrator (it delegates to the fast coder + qa/designer/po),
# not the slow general chat model — which is slow at code and whose single conversation grows too large.
_BUILD_RE = re.compile(r"\b(build|create|make|develop|deploy|scaffold|implement|code)\b.{0,45}\b(app|"
                       r"application|website|web ?app|web ?site|site|clone|web ?page|frontend|front-end|"
                       r"backend|back-end|api|server|dashboard|game|platform|tool|bot|crud)\b", re.I)


def is_build_task(text: str) -> bool:
    return bool(_BUILD_RE.search(text or ""))


def is_question(text: str) -> bool:
    """True if a plain message should be ANSWERED in chat rather than run as an agent task."""
    t = (text or "").strip()
    if not t:
        return False
    # "can you build X" / "please fix Y" is a polite command → a task, not a question
    if (_POLITE_IMPERATIVE.match(t) or re.match(r"^\s*please\s+\w", t, re.I)) and _TASK_VERB.search(t):
        return False
    if _Q_OPEN.match(t):                      # an interrogative opener wins, even if a verb appears later
        return True
    if t.endswith("?"):
        return True
    return False                              # otherwise treat it as a task (preserve agent behaviour)


def route_media(text: str) -> tuple[str, dict] | None:
    """(kind, payload) if a plain message is clearly a media-generation request, else None."""
    t = (text or "").strip()
    if not t:
        return None
    m = _SAY_RE.match(t) or _READ_RE.search(t)
    if m:
        spoken = re.sub(r"^(?:this|the following|that)\b[:,]?\s*", "", m.group(1).strip().strip('"'), flags=re.I)
        spoken = spoken.strip(" .\"")
        if spoken:
            return "tts", {"text": spoken, "params": {"voice": "af_sky"}}
    if _VID_RE.search(t):
        return "video", {"prompt": _subject(t), "params": {}, "image": None}
    if _IMG_RE.search(t):
        return "image", {"prompt": _subject(t), "params": {}, "seed": None}
    return None


def tail(text: str, n: int) -> str:
    """Last n characters of collapsed-whitespace text, with a leading ellipsis if cut."""
    text = " ".join((text or "").split())
    return text if len(text) <= n else "…" + text[-n:]


def md_to_html(md: str) -> str:
    """Convert the model's Markdown to the small HTML subset Telegram accepts (bold/italic/code/pre/
    links), escaping everything else. If the result is malformed, send() falls back to plain text, so
    this never needs to be perfect — only safe."""
    import html as _html
    md = md or ""
    ph: dict[str, str] = {}

    def _stash(val: str) -> str:
        k = f"\x00{len(ph)}\x00"
        ph[k] = val
        return k

    md = re.sub(r"```[a-zA-Z0-9_+.\-]*\n?(.*?)```",
                lambda m: _stash("<pre>" + _html.escape(m.group(1), quote=False) + "</pre>"), md, flags=re.S)
    md = re.sub(r"`([^`\n]+)`", lambda m: _stash("<code>" + _html.escape(m.group(1), quote=False) + "</code>"), md)
    md = _html.escape(md, quote=False)
    md = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', md)
    md = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", md)
    md = re.sub(r"(?m)^\s*#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", md)          # headings → bold
    md = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", md)    # *italic*
    for k, v in ph.items():
        md = md.replace(k, v)
    return md


def split_text(text: str, n: int = MAX_MSG) -> list[str]:
    text = text.strip() or "(empty)"
    parts: list[str] = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


# ── sending files ─────────────────────────────────────────────────────────
# Telegram has a method (and a form field) per media kind; we pick by file extension so a photo
# arrives as a viewable photo, audio as a playable track, video inline, everything else as a file.
PHOTO_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
PHOTO_MAX = 10 * 1024 * 1024          # Telegram rejects sendPhoto over 10 MB → fall back to a document


def media_kind(path: Path) -> tuple[str, str]:
    """(api method, form field) for a local file, chosen by extension. A too-big photo is demoted
    to a document so the send still succeeds."""
    ext = path.suffix.lower()
    try:
        big = path.stat().st_size > PHOTO_MAX
    except OSError:
        big = False
    if ext in PHOTO_EXT and not big:
        return "sendPhoto", "photo"
    if ext in AUDIO_EXT:
        return "sendAudio", "audio"
    if ext in VIDEO_EXT:
        return "sendVideo", "video"
    return "sendDocument", "document"


def multipart(fields: dict, file_field: str, filename: str, data: bytes) -> tuple[str, bytes]:
    """Build a multipart/form-data body (content-type, bytes) with one file part. No deps."""
    boundary = "law" + uuid.uuid4().hex
    crlf = b"\r\n"
    buf = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        buf += b"--" + boundary.encode() + crlf
        buf += f'Content-Disposition: form-data; name="{k}"'.encode() + crlf + crlf
        buf += str(v).encode() + crlf
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    buf += b"--" + boundary.encode() + crlf
    buf += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode() + crlf
    buf += f"Content-Type: {ctype}".encode() + crlf + crlf
    buf += data + crlf
    buf += b"--" + boundary.encode() + b"--" + crlf
    return f"multipart/form-data; boundary={boundary}", bytes(buf)


# ── Bot API client ──────────────────────────────────────────────────────
class TGError(RuntimeError):
    def __init__(self, code: int, description: str, retry_after: float | None = None):
        super().__init__(f"Telegram error {code}: {description}")
        self.code, self.description, self.retry_after = code, description, retry_after


class Api:
    def __init__(self, token: str, base: str | None = None):
        self.url = f"{(base or os.environ.get('LAW_TELEGRAM_API') or API).rstrip('/')}/bot{token}/"

    def call(self, method: str, timeout: float = 30, **params):
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(self.url + method, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except (ValueError, OSError):
                raise TGError(e.code, e.reason or "HTTP error") from None
        if not data.get("ok"):
            p = data.get("parameters") or {}
            raise TGError(int(data.get("error_code") or 0), str(data.get("description") or "unknown error"),
                          p.get("retry_after"))
        return data.get("result")

    def upload(self, method: str, file_field: str, path: Path, timeout: float = 180, **params):
        """Send a local file via multipart/form-data (sendPhoto/sendAudio/sendVideo/sendDocument)."""
        ctype, body = multipart(params, file_field, path.name, path.read_bytes())
        req = urllib.request.Request(self.url + method, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except (ValueError, OSError):
                raise TGError(e.code, e.reason or "HTTP error") from None
        if not data.get("ok"):
            p = data.get("parameters") or {}
            raise TGError(int(data.get("error_code") or 0), str(data.get("description") or "unknown error"),
                          p.get("retry_after"))
        return data.get("result")

    def get_file(self, file_id: str) -> dict:
        return self.call("getFile", file_id=file_id) or {}

    def download_file(self, file_path: str, dest: Path, timeout: float = 120) -> Path:
        """Download a file the user sent (photos/documents) via the Bot API file endpoint."""
        url = self.url.replace("/bot", "/file/bot", 1) + file_path
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            data = r.read()
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(data)
        return Path(dest)

    def get_updates(self, offset: int | None, timeout: float) -> list[dict]:
        """Long poll: waits up to `timeout` seconds for new messages."""
        return self._long_poll(offset, timeout)

    def _long_poll(self, offset, timeout) -> list[dict]:
        body = json.dumps({"offset": offset, "timeout": int(timeout), "allowed_updates": ["message"]}).encode()
        req = urllib.request.Request(self.url + "getUpdates", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=float(timeout) + 15) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except (ValueError, OSError):
                raise TGError(e.code, e.reason or "HTTP error") from None
        if not data.get("ok"):
            raise TGError(int(data.get("error_code") or 0), str(data.get("description") or "unknown error"),
                          (data.get("parameters") or {}).get("retry_after"))
        return data.get("result") or []


# ── event → progress line ───────────────────────────────────────────────
def event_line(t: str, d: dict) -> str | None:
    if t == "model":
        st, m = d.get("state"), d.get("model")
        return {"loading": f"loading {m} (~{d.get('gb')} GB)…", "ready": f"{m} ready in {d.get('load_s')}s",
                "waiting_ram": f"waiting for memory ({d.get('free_gb')} GB free, needs ~{d.get('need_gb')} GB)",
                "unloading": f"unloading {m}"}.get(st)
    if t == "warning":
        return f"⚠️ {d.get('message')}"
    if t == "snapshot":
        return f"📸 snapshot {d.get('id')} (/undo rolls back to it)"
    if t == "spec":
        return f"📝 spec: {d.get('title')} → @{d.get('assignee')}"
    if t == "probe":
        return f"probe: {d.get('message')}"
    if t == "agent":
        step = f"[{d.get('step')}]" if d.get("step") is not None else ""
        if d.get("think"):
            return f"{step} 🧠 {' '.join(str(d['think']).split())[:200]}"
        if d.get("say"):
            return f"{step} 💬 {' '.join(str(d['say']).split())[:200]}"
        if d.get("note"):
            return f"{step} · {d['note']}"
        if d.get("tool") == "delegate":
            if d.get("output"):
                return f"{step} ← sub-job {d.get('child')} ended"
            return f"{step} → @{d.get('persona')} (sub-job {d.get('child')})"
        if d.get("running"):
            return f"{step} 🔧 {d.get('tool')} {str(d.get('args', ''))[:120]}"
        first = (d.get("output") or "").strip().splitlines()
        return f"{step}    ↳ {(first[0] if first else 'done')[:120]}"
    return None


# ── the bot ─────────────────────────────────────────────────────────────
class Bot:
    def __init__(self, paths: Paths, reg, q, api: Api, users: set[int], cfg: dict | None = None):
        self.paths, self.reg, self.q, self.api, self.users = paths, reg, q, api, set(users)
        self.cfg = {**DEFAULTS, **((reg.raw.get("runtime") or {}).get("telegram") or {}), **(cfg or {})}
        self.file = paths.state / "telegram.json"
        try:
            self.state = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.state = {}
        for k, v in (("offset", None), ("chats", {}), ("follows", {}), ("refused", []),
                     ("schedules", []), ("sched_seq", 0)):
            self.state.setdefault(k, v)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()

    # ── state ───────────────────────────────────────────────────────────
    def save(self) -> None:
        with self.lock:
            atomic_write(self.file, json.dumps(self.state, indent=2, sort_keys=True) + "\n", mode=0o600)

    def chat(self, cid: int) -> dict:
        return self.state["chats"].setdefault(str(cid), {"persona": self.cfg["persona"],
                                                         "project": self.cfg["project"]})

    def push_history(self, cid: int, role: str, content: str) -> None:
        """Record one turn of the chat so later messages ('it's not working') have context."""
        h = self.chat(cid).setdefault("history", [])
        h.append({"role": role, "content": " ".join(str(content or "").split())[:HISTORY_CHARS]})
        del h[:-HISTORY_TURNS]
        with self.lock:
            self.save()

    def history(self, cid: int) -> list[dict]:
        return list(self.chat(cid).get("history") or [])

    def project_path(self, ch: dict) -> Path:
        p = str(ch.get("project") or self.cfg["project"])
        if p.startswith("~") or p.startswith("/"):
            return Path(p).expanduser().resolve()
        return (self.paths.source / p).resolve()

    # ── sending ─────────────────────────────────────────────────────────
    def _api(self, method: str, **params):
        for attempt in range(3):
            try:
                return self.api.call(method, **params)
            except TGError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(min(float(e.retry_after or 2), 30))
                    continue
                raise

    def action(self, cid: int, kind: str = "typing") -> None:
        """Show the '… is typing' indicator in the chat (lasts ~5s; re-send to keep it on)."""
        try:
            self._api("sendChatAction", chat_id=cid, action=kind)
        except (TGError, OSError, urllib.error.URLError):
            pass

    def send(self, cid: int, text: str) -> int | None:
        mid = None
        for part in split_text(text):
            try:
                r = self._api("sendMessage", chat_id=cid, text=md_to_html(part), parse_mode="HTML",
                              disable_web_page_preview=True)
                mid = (r or {}).get("message_id")
            except TGError as e:
                # a bad entity conversion → resend the raw text with no formatting (never drop a message)
                if "parse" in (e.description or "").lower() or "entity" in (e.description or "").lower() \
                        or "tag" in (e.description or "").lower():
                    try:
                        r = self._api("sendMessage", chat_id=cid, text=part, disable_web_page_preview=True)
                        mid = (r or {}).get("message_id")
                        continue
                    except (TGError, OSError, urllib.error.URLError) as e2:
                        log.warning("sendMessage (plain fallback) failed: %s", e2)
                else:
                    log.warning("sendMessage failed: %s", e)
            except (OSError, urllib.error.URLError) as e:
                log.warning("sendMessage failed: %s", e)
        return mid

    def send_file(self, cid: int, path: str | Path, caption: str = "") -> bool:
        """Upload a local file to the chat as the right media kind (photo/audio/video/document).
        Returns True on success. The caption is trimmed to Telegram's 1024-char limit."""
        p = Path(path).expanduser()
        if not p.is_file():
            log.warning("send_file: %s is not a file", p)
            return False
        method, field = media_kind(p)
        params = {"chat_id": cid}
        if caption:
            params["caption"] = caption[:1024]
        for attempt in range(3):
            try:
                self.api.upload(method, field, p, **params)
                return True
            except TGError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(min(float(e.retry_after or 2), 30))
                    continue
                # a document always works even when sendPhoto/Video is fussy — try once as a fallback
                if method != "sendDocument" and attempt == 0:
                    method, field = "sendDocument", "document"
                    continue
                log.warning("send_file(%s) failed: %s", p.name, e)
                return False
            except (OSError, urllib.error.URLError) as e:
                log.warning("send_file(%s) failed: %s", p.name, e)
                return False
        return False

    def edit(self, cid: int, mid: int | None, text: str) -> None:
        if mid is None:
            self.send(cid, text)
            return
        try:
            self._api("editMessageText", chat_id=cid, message_id=mid, text=split_text(text)[-1][:MAX_MSG],
                      disable_web_page_preview=True)
        except TGError as e:
            if "not modified" not in e.description:
                log.warning("editMessageText failed: %s", e)
        except (OSError, urllib.error.URLError) as e:
            log.warning("editMessageText failed: %s", e)

    # ── polling ─────────────────────────────────────────────────────────
    def skip_backlog(self) -> None:
        """First start: don't act on messages sent before the bot existed."""
        if self.state.get("offset") is not None:
            return
        try:
            ups = self.api.get_updates(-1, 0)
        except (TGError, OSError, urllib.error.URLError) as e:
            log.warning("could not read the backlog: %s", e)
            return
        self.state["offset"] = (ups[-1]["update_id"] + 1) if ups else 0
        self.save()

    def poll_once(self, timeout: float | None = None) -> int:
        timeout = self.cfg["poll_s"] if timeout is None else timeout
        ups = self.api.get_updates(self.state.get("offset"), timeout)
        for u in ups:
            with self.lock:
                self.state["offset"] = u["update_id"] + 1
                self.save()
            try:
                self.handle(u)
            except Exception as e:  # noqa: BLE001 — one bad message must not stop the bot
                log.exception("handling update failed")
                cid = ((u.get("message") or {}).get("chat") or {}).get("id")
                if cid is not None:
                    self.send(cid, f"Something went wrong: {e}")
        return len(ups)

    # ── incoming message ────────────────────────────────────────────────
    def handle(self, update: dict) -> None:
        m = update.get("message") or {}
        cid = (m.get("chat") or {}).get("id")
        uid = (m.get("from") or {}).get("id")
        if cid is None:
            return
        if uid not in self.users:
            log.warning("ignored a message from user %s (not in TELEGRAM_USER_ID)", uid)
            if uid not in self.state["refused"]:
                self.state["refused"].append(uid)
                self.save()
                self.send(cid, f"This bot is private. (Your Telegram user id is {uid}.)")
            return
        photo = m.get("photo")
        doc = m.get("document")
        if photo or (doc and str(doc.get("mime_type") or "").startswith("image/")):
            self.handle_photo(cid, m)
            return
        voice = m.get("voice") or m.get("audio")
        if voice:
            self.handle_voice(cid, voice)
            return
        text = (m.get("text") or "").strip()
        if not text:
            self.send(cid, "Send me text, a voice note, or a photo. (Other files come later.)")
            return
        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            name = head[1:].split("@")[0].lower()
            fn = getattr(self, f"cmd_{name}", None)
            if fn is None:
                self.send(cid, f"Unknown command /{name}. /help lists them.")
                return
            fn(cid, arg.strip())
        else:
            self.route_text(cid, text)

    def route_text(self, cid: int, text: str) -> None:
        """Route a plain message (typed OR transcribed from voice): media generation, a quick chat
        answer, or an agent task — with mode-aware follow-ups."""
        routed = route_media(text)
        if routed:
            kind, payload = routed
            label = {"image": "🖼 image", "tts": "🔊 voice", "video": "🎬 video"}[kind]
            subj = payload.get("prompt") or payload.get("text") or ""
            self._enqueue_media(cid, kind, payload, f"{label} · {subj[:55]}")
            return
        lm = self.chat(cid).get("last_media")
        if (self.chat(cid).get("last_mode") == "media" and lm and lm.get("kind") in ("image", "video")
                and is_media_edit(text)):
            kind = lm["kind"]
            new_prompt = f"{lm.get('prompt', '')}, {text}".strip(", ").strip()
            payload = {"prompt": new_prompt, "params": {}}
            payload["image" if kind == "video" else "seed"] = None
            label = {"image": "🖼 image", "video": "🎬 video"}[kind]
            self._enqueue_media(cid, kind, payload, f"{label} (edit) · {text[:45]}")
            return
        if is_question(text):
            self.answer(cid, text)                        # a question → quick streamed answer, no tools
        elif _TASK_VERB.search(text):
            self.start_agent(cid, text)                   # a clear task verb → agent with tools
        elif self.chat(cid).get("last_mode") == "chat" and self.history(cid):
            self.answer(cid, text)                        # a follow-up to the chat ("it's not working")
        elif self.chat(cid).get("last_mode") == "task" and self.history(cid) and len(text) > 30:
            self.start_agent(cid, text)                   # a substantive follow-up to a task
        else:
            self.answer(cid, text)                        # casual/short message ("bro", "thanks") → just chat

    # ── jobs ────────────────────────────────────────────────────────────
    def handle_voice(self, cid: int, voice: dict) -> int | None:
        """A voice note / audio → download it and transcribe it (whisper). When the transcript is ready
        (finish()), it is routed exactly like a typed message."""
        self.action(cid, "typing")
        try:
            fp = self.api.get_file(voice["file_id"]).get("file_path") or ""
            ext = Path(fp).suffix or ".oga"
            dest = self.paths.outputs / "voice-in" / f"tg-{int(time.time())}{ext}"
            self.api.download_file(fp, dest)
        except (TGError, OSError, urllib.error.URLError, KeyError) as e:
            self.send(cid, f"Couldn't download that voice note: {e}")
            return None
        jid = self.q.enqueue("whisper", {"audio": str(dest)}, persona=None, source="telegram", kind="stt")
        self.follow(cid, jid, "stt", f"job {jid} · 🎤 transcribing…")
        with self.lock:
            self.chat(cid)["last_job"] = jid
            self.save()
        return jid

    def handle_photo(self, cid: int, m: dict) -> int | None:
        """A photo (or image document) → download it and analyse it with the vision model. The caption
        is the question ('where is this?'); without one, a general description is given."""
        photo, doc = m.get("photo"), m.get("document")
        file_id = (photo[-1]["file_id"] if photo else doc["file_id"])   # largest size
        caption = (m.get("caption") or "").strip()
        self.action(cid, "typing")
        try:
            fp = self.api.get_file(file_id).get("file_path") or ""
            ext = Path(fp).suffix or (".jpg" if photo else ".bin")
            dest = self.paths.outputs / "vision" / f"tg-{int(time.time())}{ext}"
            self.api.download_file(fp, dest)
        except (TGError, OSError, urllib.error.URLError) as e:
            self.send(cid, f"Couldn't download that image: {e}")
            return None
        jid = self.q.enqueue("qwen3-vl-8b", {"image": str(dest), "prompt": caption},
                             persona="vision", source="telegram", kind="vision")
        self.follow(cid, jid, "vision", f"job {jid} · 👁 analysing image" + (f" · {caption[:50]}" if caption else ""))
        self.push_history(cid, "user", f"[sent a photo]{': ' + caption if caption else ''}")
        with self.lock:
            self.chat(cid)["last_job"] = jid
            self.chat(cid)["last_mode"] = "chat"
            self.save()
        return jid

    def start_agent(self, cid: int, task: str, *, translate: bool | None = None,
                    spec_text: str | None = None, plan_only: bool = False) -> int | None:
        from .agent.runner import AgentRefused, agent_cfg, check_project
        from .project import ensure_project
        ch = self.chat(cid)
        persona = ch.get("persona") or self.cfg["persona"]
        if persona not in self.reg.personas:
            persona = self.cfg["persona"]
        # a software build → orchestrator (delegates to the fast coder + qa/designer/po), unless the
        # user explicitly chose a persona other than the default
        if (is_build_task(task) and ch.get("persona") in (None, "", self.cfg["persona"])
                and "orchestrator" in self.reg.personas):
            persona = "orchestrator"
        try:
            project = check_project(self.project_path(ch), self.paths.home)
            ensure_project(project)
        except (AgentRefused, OSError) as e:
            self.send(cid, f"Can't use that project folder: {e}\nChange it with /project <name>.")
            return None
        # give the agent the recent conversation so a follow-up ("it's still broken") has context
        task_for_agent = task
        hist = self.history(cid)
        if hist and not spec_text and not plan_only:
            convo = "\n".join(f"{m['role']}: {m['content']}" for m in hist[-6:])
            task_for_agent = (f"The user is messaging you on Telegram. Recent conversation (they may be "
                              f"following up on it):\n{convo}\n\nTheir new message: {task}")
        payload = {"task": task_for_agent, "cwd": str(project), "depth": 0, "telegram_chat_id": cid,
                   "translate": agent_cfg(self.reg)["translate"] if translate is None else translate,
                   "messages": [{"role": "user", "content": task_for_agent}]}
        if spec_text:
            payload.update(spec_text=spec_text, translate=False)
        if plan_only:
            payload.update(plan_only=True, translate=True)
        if not plan_only:
            self.push_history(cid, "user", task)
        model = self.reg.personas[persona]["model"]
        jid = self.q.enqueue(model, payload, persona=persona, source="telegram", kind="agent")
        what = "plan" if plan_only else "agent"
        self.follow(cid, jid, what, f"job {jid} · {what} · @{persona} → {model}\n📁 {project}")
        with self.lock:
            ch["last_mode"] = "task"
            ch["last_job"] = jid
            self.save()
        return jid

    def follow(self, cid: int, jid: int, kind: str, header: str) -> None:
        mid = self.send(cid, f"{header}\n{ICON['queued']} queued")
        with self.lock:
            self.state["follows"][str(jid)] = {"chat": cid, "msg": mid, "after": 0, "kind": kind,
                                               "header": header, "lines": [], "status": "queued",
                                               "t0": time.time(), "last_edit": 0, "shown": "",
                                               "stream": "", "think": "", "last_action": 0}
            self.save()

    def _live_line(self, f: dict) -> str | None:
        """The model's in-progress reply (live thinking, then the live answer) for the current
        turn, from streamed deltas — the 'watch it think' line, like web chat with thinking on."""
        raw = f.get("stream") or ""
        think_rc = f.get("think") or ""            # reasoning_content, if the server sends it split
        thinking, answer = live_split(raw)
        thinking = (think_rc + " " + thinking).strip() if think_rc else thinking
        if answer:
            return "💬 " + tail(answer, 360)
        if thinking:
            return "🧠 " + tail(thinking, 360)
        return None

    def _render(self, f: dict, status: str) -> str:
        lines = f["lines"][-int(self.cfg["progress_lines"]):]
        live = self._live_line(f) if status in ("running", "waiting") else None
        if live:
            lines = (lines + [live])[-int(self.cfg["progress_lines"]):]
        secs = time.time() - f["t0"]
        return f"{f['header']}\n{ICON.get(status, '•')} {status} · {secs:.0f}s" + \
            ("\n\n" + "\n".join(lines) if lines else "")

    def pump(self) -> None:
        """Update every followed job's progress message; finish the ones that ended."""
        with self.lock:
            follows = list(self.state["follows"].items())
        for key, f in follows:
            jid = int(key)
            end = None
            for e in self.q.events(jid, f["after"]):
                f["after"] = e["seq"]
                t, d = e["type"], e["data"] or {}
                if t == "status":
                    f["status"] = d.get("state") or f["status"]
                elif t in ("done", "error", "cancelled"):
                    end = t
                elif t == "deltas":
                    for delta in (d if isinstance(d, list) else []):
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        if rc:
                            f["think"] = ((f.get("think") or "") + rc)[-1500:]
                        if delta.get("content"):
                            s = (f.get("stream") or "") + delta["content"]
                            if len(s) > 6000:        # stay small, but keep the </think> boundary
                                if "</think>" in s:
                                    s = "</think>" + s.split("</think>", 1)[1][-5000:]
                                else:
                                    s = s[-5000:]
                            f["stream"] = s
                else:
                    line = event_line(t, d)
                    if line:
                        f["lines"].append(line)
                        f["lines"] = f["lines"][-50:]
                    # a committed step event (think / say / tool / spec) ends the live turn:
                    # the recorded line replaces the streamed preview so it isn't shown twice
                    if t in ("agent", "spec"):
                        f["stream"], f["think"] = "", ""
            if end is None and self.q.status(jid) in ("done", "failed", "cancelled"):
                end = {"done": "done", "failed": "error", "cancelled": "cancelled"}[self.q.status(jid)]
            if end:
                self.finish(jid, f, end)
                with self.lock:
                    self.state["follows"].pop(key, None)
                    self.save()
                continue
            if f["status"] in ("running", "waiting") and time.time() - f.get("last_action", 0) >= 4:
                self.action(f["chat"], "typing")          # keep the "… is typing" indicator alive
                f["last_action"] = time.time()
            text = self._render(f, f["status"])
            if text != f["shown"] and time.time() - f["last_edit"] >= float(self.cfg["edit_every_s"]):
                self.edit(f["chat"], f["msg"], text)
                f["shown"], f["last_edit"] = text, time.time()
            with self.lock:
                if key in self.state["follows"]:
                    self.state["follows"][key] = f
                    self.save()

    def finish(self, jid: int, f: dict, end: str) -> None:
        from .agent.textcalls import split_think
        cid = f["chat"]
        job = self.q.get(jid) or {}
        if end == "done":
            try:
                res = json.loads(job.get("result") or "{}")
            except ValueError:
                res = {}
            ag = res.get("agent") or {}
            extra = f" · {ag.get('steps')} steps · {ag.get('tools_used', 0)} tool calls" if ag else ""
            self.edit(cid, f["msg"], f"{f['header']}\n✅ done in {time.time() - f['t0']:.0f}s{extra}")
            # media jobs deliver the generated file itself (photo / audio / video), not a path string
            sent_media = False
            for key, label in (("image", "🖼 image"), ("video", "🎬 video"), ("tts", "🔊 audio")):
                info = res.get(key) or {}
                mp = info.get("path")
                if mp and Path(mp).exists():
                    cap = label + (f" · {info['model']}" if info.get("model") else "") + \
                        (f" · {info['seconds']}s" if info.get("seconds") is not None else "")
                    if not self.send_file(cid, mp, cap):
                        self.send(cid, f"(generated, but couldn't attach it — saved at {mp})")
                    sent_media = True
            if sent_media:
                return
            content = split_think((res.get("message") or {}).get("content") or "")[0]
            if f["kind"] == "stt":                        # a voice note → show the words, then act on them
                transcript = (content or "").strip()
                if not transcript:
                    self.send(cid, "🎤 (couldn't make out any speech — try again?)")
                    return
                self.send(cid, f'🎤 "{transcript}"')
                self.route_text(cid, transcript)
                return
            if f["kind"] == "plan":
                plan = res.get("plan") or {}
                if plan.get("spec"):
                    with self.lock:
                        self.chat(cid)["pending_plan"] = plan
                        self.save()
                    self.send(cid, plan["spec"])
                    self.send(cid, "Reply /go to run this spec, /raw to run your words as written, "
                                   "or send /plan again with changes.")
                else:
                    self.send(cid, "The translator gave no usable spec. Use /raw <task> to run it as written.")
                return
            self.send(cid, content or "(no answer)")
            if content and f["kind"] in ("chat", "agent", "vision"):
                self.push_history(cid, "assistant", content)   # remember the reply for follow-ups
            if ag.get("snapshot"):
                self.send(cid, f"↩️ /undo rolls {ag.get('project')} back to before this job.")
        elif end == "error":
            self.edit(cid, f["msg"], f"{f['header']}\n❌ failed after {time.time() - f['t0']:.0f}s")
            msg = f"❌ job {jid} failed: {job.get('error') or 'unknown error'}"
            if job.get("kind") == "agent":
                msg += f"\n/resume {jid} continues it."
            self.send(cid, msg)
        else:
            self.edit(cid, f["msg"], f"{f['header']}\n⏹ cancelled ({job.get('error') or ''})")

    # ── commands ────────────────────────────────────────────────────────
    def cmd_start(self, cid: int, arg: str) -> None:
        self.send(cid, HELP)

    cmd_help = cmd_start

    def _web_lookup(self, query: str, n: int = 5) -> str:
        """Top SearXNG results as plain text, for grounding a chat answer in current facts."""
        port = int(self.reg.ports.get("searxng", 8888))
        url = f"http://127.0.0.1:{port}/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "safesearch": 0})
        with urllib.request.urlopen(url, timeout=18) as r:
            data = json.loads(r.read())
        rows = data.get("results") or []
        return "\n".join(f"- {x.get('title', '').strip()}: {(x.get('content') or '').strip()[:220]} "
                         f"({x.get('url', '')})" for x in rows[:n])

    def answer(self, cid: int, question: str) -> int:
        """Answer a question in chat: a quick, streamed reply (thinking shown live, no tools). The
        recent conversation is included so follow-ups ('it's not working') keep their context, and a
        question about current facts is grounded with a quick web search."""
        ch = self.chat(cid)
        # orchestrator/coder etc. are build personas; a plain question is answered by a chat persona
        persona = ch.get("persona") if ch.get("persona") in self.reg.personas else self.cfg["persona"]
        if persona in ("orchestrator", "coder", "qa", "designer", "po"):
            persona = self.cfg["persona"]
        model = self.reg.personas[persona]["model"]
        self.push_history(cid, "user", question)
        sysbits = [f"Today is {time.strftime('%A, %d %B %Y')}. You are a helpful assistant replying on "
                   "Telegram; be natural and concise."]
        if needs_lookup(question):
            self.action(cid, "typing")
            try:
                hits = self._web_lookup(question)
                if hits:
                    sysbits.append("Web search results just now (use them to answer; you DO have current "
                                   "info via these):\n" + hits)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        messages = [{"role": "system", "content": "\n\n".join(sysbits)}] + self.history(cid)
        # Qwen3's chat template opens <think> in the prompt only when thinking is enabled; turning it on
        # makes the reply stream its reasoning then '</think>' then the answer, so the thought process is
        # shown live (live_split) and the final answer is clean (split_think strips it).
        params = {"chat_template_kwargs": {"enable_thinking": True}}
        jid = self.q.enqueue(model, {"messages": messages, "params": params}, persona=persona, source="telegram")
        self.follow(cid, jid, "chat", f"job {jid} · 💬 ask · @{persona} → {model}")
        with self.lock:
            ch["last_job"] = jid
            ch["last_mode"] = "chat"
            self.save()
        return jid

    def cmd_ask(self, cid: int, arg: str) -> None:
        if not arg:
            self.send(cid, "Usage: /ask <question>")
            return
        self.answer(cid, arg)

    def cmd_plan(self, cid: int, arg: str) -> None:
        if not arg:
            self.send(cid, "Usage: /plan <task> — shows the task spec; /go then runs it.")
            return
        self.chat(cid)["plan_task"] = arg
        self.start_agent(cid, arg, plan_only=True)

    def cmd_go(self, cid: int, arg: str) -> None:
        plan = self.chat(cid).get("pending_plan")
        if not plan:
            self.send(cid, "No plan waiting. Send /plan <task> first.")
            return
        with self.lock:
            self.chat(cid).pop("pending_plan", None)
            self.save()
        self.start_agent(cid, plan["task"], spec_text=plan["spec"])

    def cmd_raw(self, cid: int, arg: str) -> None:
        if not arg:
            plan = self.chat(cid).get("pending_plan")
            if plan:
                with self.lock:
                    self.chat(cid).pop("pending_plan", None)
                    self.save()
                self.start_agent(cid, plan["task"], translate=False)
                return
            self.send(cid, "Usage: /raw <task>")
            return
        self.start_agent(cid, arg, translate=False)

    _MEDIA_MODEL = {"image": "flux", "tts": "kokoro", "video": "ltx23"}

    def _enqueue_media(self, cid: int, kind: str, payload: dict, header: str) -> int:
        jid = self.q.enqueue(self._MEDIA_MODEL[kind], payload, persona=None, source="telegram", kind=kind)
        self.follow(cid, jid, kind, f"job {jid} · {header}")
        with self.lock:
            ch = self.chat(cid)
            ch["last_job"] = jid
            ch["last_mode"] = "media"
            ch["last_media"] = {"kind": kind, "prompt": payload.get("prompt") or payload.get("text") or ""}
            self.save()
        return jid

    def cmd_image(self, cid: int, arg: str) -> None:
        if not arg:
            self.send(cid, "Usage: /image <prompt> — generates an image and sends it here.")
            return
        self._enqueue_media(cid, "image", {"prompt": arg, "params": {}, "seed": None}, f"🖼 image · {arg[:60]}")

    def cmd_voice(self, cid: int, arg: str) -> None:
        if not arg:
            self.send(cid, "Usage: /voice [voice] <text> — speaks the text and sends the audio.\n"
                           "Voices: " + ", ".join(sorted(VOICES)))
            return
        head, _, rest = arg.partition(" ")
        voice, text = (head, rest.strip()) if head in VOICES and rest.strip() else ("af_sky", arg)
        self._enqueue_media(cid, "tts", {"text": text, "params": {"voice": voice}},
                            f"🔊 voice ({voice}) · {text[:50]}")

    def cmd_video(self, cid: int, arg: str) -> None:
        if not arg:
            self.send(cid, "Usage: /video <prompt> — generates a short video (slow; first run downloads models).")
            return
        self._enqueue_media(cid, "video", {"prompt": arg, "params": {}, "image": None}, f"🎬 video · {arg[:60]}")

    # ── scheduled / proactive tasks ─────────────────────────────────────
    def _parse_when(self, s: str) -> tuple[str | None, float | str | None]:
        """'in 30m' / 'in 2h' → ('once', epoch); 'HH:MM' → ('once', next epoch at that time today/tomorrow)."""
        import datetime
        s = s.strip().lower()
        m = re.match(r"in\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\b", s)
        if m:
            n = int(m.group(1))
            return "once", time.time() + (n * 60 if m.group(2).startswith("m") else n * 3600)
        m = re.match(r"(\d{1,2}):(\d{2})$", s)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            now = datetime.datetime.now()
            t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            return "once", t.timestamp()
        return None, None

    def _add_schedule(self, cid: int, kind: str, at, text: str) -> int:
        with self.lock:
            self.state["sched_seq"] += 1
            sid = self.state["sched_seq"]
            self.state["schedules"].append({"id": sid, "chat": cid, "kind": kind, "at": at,
                                            "text": text, "last_day": ""})
            self.save()
        return sid

    def cmd_remind(self, cid: int, arg: str) -> None:
        m = re.match(r"(in\s+\d+\s*\w+|\d{1,2}:\d{2})\s+(.+)", arg.strip(), re.I | re.S)
        if not m:
            self.send(cid, "Usage: /remind <in 30m | in 2h | 14:30> <what to remind you>")
            return
        kind, when = self._parse_when(m.group(1))
        if not kind:
            self.send(cid, "Couldn't read that time. Try: /remind in 30m <what>  or  /remind 14:30 <what>")
            return
        sid = self._add_schedule(cid, "once", when, m.group(2).strip())
        self.send(cid, f"⏰ Reminder #{sid} set for {time.strftime('%H:%M', time.localtime(when))}.")

    def cmd_daily(self, cid: int, arg: str) -> None:
        m = re.match(r"(\d{1,2}):(\d{2})\s+(.+)", arg.strip(), re.S)
        if not m:
            self.send(cid, "Usage: /daily <HH:MM> <task to run every day> "
                           "(e.g. /daily 08:00 summarise the top AI news today)")
            return
        hm = f"{int(m.group(1)):02d}:{m.group(2)}"
        sid = self._add_schedule(cid, "daily", hm, m.group(3).strip())
        self.send(cid, f"🔁 Daily task #{sid} set for {hm} every day.")

    def cmd_schedules(self, cid: int, arg: str) -> None:
        rows = [s for s in self.state["schedules"] if s["chat"] == cid]
        if not rows:
            self.send(cid, "No reminders or daily tasks. Set one with /remind or /daily.")
            return
        lines = []
        for s in rows:
            when = s["at"] if s["kind"] == "daily" else time.strftime("%a %H:%M", time.localtime(s["at"]))
            icon = "🔁" if s["kind"] == "daily" else "⏰"
            lines.append(f"#{s['id']} {icon} {when} — {s['text'][:60]}")
        self.send(cid, "\n".join(lines) + "\n\nRemove one with /unschedule <id>.")

    def cmd_unschedule(self, cid: int, arg: str) -> None:
        if not arg.strip().isdigit():
            self.send(cid, "Usage: /unschedule <id> (see /schedules)")
            return
        sid = int(arg.strip())
        with self.lock:
            before = len(self.state["schedules"])
            self.state["schedules"] = [s for s in self.state["schedules"]
                                       if not (s["id"] == sid and s["chat"] == cid)]
            self.save()
        self.send(cid, f"Removed #{sid}." if len(self.state["schedules"]) < before else f"No schedule #{sid}.")

    def _fire_schedule(self, s: dict) -> None:
        cid, text = s["chat"], s["text"]
        if s["kind"] == "daily":
            self.send(cid, f"🔁 Running your daily task: {text}")
            self.route_text(cid, text)                    # execute it (question/task/media)
        else:
            self.send(cid, f"⏰ Reminder: {text}")

    def _due_schedules(self) -> None:
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        hm = time.strftime("%H:%M")
        fire, keep = [], []
        with self.lock:
            for s in self.state["schedules"]:
                if s["kind"] == "once" and now >= float(s["at"]):
                    fire.append(s)
                elif s["kind"] == "daily" and s["at"] == hm and s.get("last_day") != today:
                    s["last_day"] = today
                    fire.append(s)
                    keep.append(s)
                else:
                    keep.append(s)
            if fire:
                self.state["schedules"] = keep
                self.save()
        for s in fire:
            try:
                self._fire_schedule(s)
            except Exception:  # noqa: BLE001
                log.exception("firing schedule %s failed", s.get("id"))

    def _scheduler_loop(self) -> None:
        while not self.stop_event.wait(20.0):
            try:
                self._due_schedules()
            except Exception:  # noqa: BLE001
                log.exception("scheduler loop error")

    def cmd_status(self, cid: int, arg: str) -> None:
        from .runtime import memory
        w = self.q.worker_info()
        if not w or w["state"] == "stopped" or w["age_s"] > 30:
            wl = "worker: NOT running (run `law start` on the Mac)"
        else:
            wl = f"worker: {w['state']} · model {w['model'] or '-'}" + \
                 (f" · job {w['job_id']}" if w.get("job_id") else "")
        ch = self.chat(cid)
        active = [j for j in self.q.jobs(50) if j["status"] in ("queued", "running", "waiting")]
        lines = [wl, f"queue: {self.q.depth()} waiting",
                 f"memory: {memory.available_gb():.0f} GB free · pressure {memory.pressure()}",
                 f"persona: {ch.get('persona')} · project: {self.project_path(ch)}"]
        if active:
            lines.append("active: " + ", ".join(f"{j['id']} ({j['status']})" for j in active[:8]))
        self.send(cid, "\n".join(lines))

    def cmd_jobs(self, cid: int, arg: str) -> None:
        rows = []
        for j in self.q.jobs(8):
            try:
                p = json.loads(j["payload"])
                what = p.get("task") or p["messages"][-1]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                what = ""
            what = " ".join(str(what).split())[:50]
            rows.append(f"{j['id']} {ICON.get(j['status'], '')} {j['status']} · {j.get('kind')} · "
                        f"@{j.get('persona') or j['model']} · {what}")
        self.send(cid, "\n".join(rows) or "no jobs yet")

    def _job_arg(self, cid: int, arg: str) -> int | None:
        if arg.strip().isdigit():
            return int(arg.strip())
        return self.chat(cid).get("last_job")

    def cmd_cancel(self, cid: int, arg: str) -> None:
        jid = self._job_arg(cid, arg)
        if jid is None:
            self.send(cid, "Usage: /cancel <job id>")
            return
        if self.q.cancel(jid, reason="cancelled from Telegram"):
            self.send(cid, f"⏹ job {jid} cancelled.")
        else:
            self.send(cid, f"Job {jid} is not running (status: {self.q.status(jid)}).")

    def cmd_resume(self, cid: int, arg: str) -> None:
        jid = self._job_arg(cid, arg)
        job = self.q.get(jid) if jid is not None else None
        if not job:
            self.send(cid, "Usage: /resume <job id>")
            return
        if job.get("kind") != "agent":
            self.send(cid, f"Job {jid} is a {job.get('kind')} job — only agent jobs can be resumed.")
            return
        if job["status"] == "done":
            self.send(cid, f"Job {jid} already finished.")
            return
        if job["status"] in ("failed", "cancelled") and not self.q.revive(jid):
            self.send(cid, f"Could not resume job {jid}.")
            return
        st = self.q.get_state(jid) or {}
        self.follow(cid, jid, "agent", f"job {jid} · resumed from step {st.get('step', 0)} · @{job.get('persona')}")

    def cmd_undo(self, cid: int, arg: str) -> None:
        from .undo import Undo, UndoError
        jid = self._job_arg(cid, arg)
        job = self.q.get(jid) if jid is not None else None
        snap = project = None
        if job and job.get("kind") == "agent":
            st = self.q.get_state(jid) or {}
            snap, project = st.get("snapshot"), st.get("project")
        if not snap or not project:
            self.send(cid, "Nothing to undo — no agent job with a snapshot in this chat yet.")
            return
        try:
            res = Undo(Path(project)).rollback(snap)
        except UndoError as e:
            self.send(cid, f"Undo failed: {e}")
            return
        if not res["changed"]:
            self.send(cid, f"{project} already matches snapshot {snap} — nothing to undo.")
            return
        files = "\n".join(res["changed"][:15]) + (f"\n… and {len(res['changed']) - 15} more"
                                                  if len(res["changed"]) > 15 else "")
        self.send(cid, f"↩️ restored {project} to snapshot {snap} (before job {jid}):\n{files}\n\n"
                       f"Changed your mind? On the Mac: law rollback {project} --to {res['safety']}")

    def cmd_persona(self, cid: int, arg: str) -> None:
        ch = self.chat(cid)
        if not arg:
            rows = [f"{'→' if n == ch.get('persona') else ' '} {n} ({p['model']}) — {p.get('desc', '')}"
                    for n, p in self.reg.personas.items()]
            self.send(cid, "Persona for plain messages:\n" + "\n".join(rows) + "\n\nChange: /persona <name>")
            return
        if arg not in self.reg.personas:
            self.send(cid, f"Unknown persona '{arg}'. /persona lists them.")
            return
        with self.lock:
            ch["persona"] = arg
            self.save()
        self.send(cid, f"Plain messages now go to @{arg} ({self.reg.personas[arg]['model']}).")

    def cmd_project(self, cid: int, arg: str) -> None:
        from .agent.runner import AgentRefused, check_project
        ch = self.chat(cid)
        if not arg:
            self.send(cid, f"Project folder: {self.project_path(ch)}\nChange: /project <name or path>")
            return
        old = ch.get("project")
        ch["project"] = arg
        try:
            path = check_project(self.project_path(ch), self.paths.home)
        except AgentRefused as e:
            ch["project"] = old
            self.send(cid, str(e))
            return
        with self.lock:
            self.save()
        self.send(cid, f"Project folder: {path}" + ("" if path.exists() else " (created on the first task)"))

    # ── main loop ───────────────────────────────────────────────────────
    def touch(self) -> None:
        f = beat_file(self.paths)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(time.time()))

    def _pump_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                self.pump()
            except Exception:  # noqa: BLE001
                log.exception("progress update failed")

    def run_forever(self) -> None:
        try:
            me = self.api.call("getMe") or {}
            self.state["bot"] = me.get("username")
            self.save()
            log.info("telegram bot @%s ready (users %s)", me.get("username"), sorted(self.users))
        except (TGError, OSError, urllib.error.URLError) as e:
            log.error("getMe failed: %s", e)
        self.skip_backlog()
        threading.Thread(target=self._pump_loop, name="pump", daemon=True).start()
        threading.Thread(target=self._scheduler_loop, name="scheduler", daemon=True).start()
        backoff = 1.0
        while not self.stop_event.is_set():
            self.touch()
            try:
                self.poll_once()
                backoff = 1.0
            except TGError as e:
                self.touch()                      # stay alive for the watchdog through error storms
                if e.code == 401:
                    log.error("Telegram rejected the bot token — fix it with `law configure`")
                    self.stop_event.wait(60)
                elif e.code == 409:
                    log.error("another program is reading this bot's messages (409) — only one may run")
                    self.stop_event.wait(30)
                else:
                    log.warning("getUpdates: %s", e)
                    self.stop_event.wait(e.retry_after or backoff)
                    backoff = min(backoff * 2, 12)
            except (OSError, urllib.error.URLError, ValueError) as e:
                # api.telegram.org can reset the long-poll connection repeatedly on a flaky link; keep the
                # heartbeat fresh and back off with jitter so the watchdog doesn't mistake this for a freeze.
                self.touch()
                log.warning("network problem: %s (retrying in %.0fs)", e, backoff)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 12) * (0.7 + 0.6 * random.random())
            except Exception:  # noqa: BLE001 — nothing unexpected may kill the poll loop (the bot is the UI)
                self.touch()
                log.exception("unexpected error in poll loop (continuing)")
                self.stop_event.wait(min(backoff, 10))
                backoff = min(backoff * 2, 12)


def build(paths: Paths | None = None) -> Bot | None:
    from .runtime.db import Queue
    paths = paths or get_paths()
    token, users = creds(paths)
    if not token or not users:
        return None
    return Bot(paths, load(paths), Queue(paths.queue_db), Api(token), users)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s telegram %(levelname)s %(message)s")
    bot = build()
    if bot is None:
        log.error("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_USER_ID) — run `law configure`")
        while True:                       # don't let launchd restart us in a tight loop
            time.sleep(3600)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: bot.stop_event.set())
    bot.run_forever()


if __name__ == "__main__":
    main()
