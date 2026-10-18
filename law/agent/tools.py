"""Agent tools — what an agent can DO on the Mac.

    run_shell   any shell command, in the project folder (full access — your Mac, your call)
    read_file   read a text file (optionally a line range)
    write_file  create / overwrite a file (folders are created)
    edit_file   replace an exact piece of text in a file (safer than rewriting it)
    list_dir    list a folder (recursive up to a depth)
    web_search  search the web through your private SearXNG
    web_fetch   download a web page as plain text
    remember    save a durable fact to the project's memory (central store, not a file)
    delegate    hand a sub-task to another persona (handled by the runner, not here)

There are NO approval prompts. Safety comes from undo: a snapshot of the project is taken
before every agent run (`law rollback`). Only a tiny hard-block list is refused outright,
because no snapshot can undo it: deleting / or your home folder, erasing/partitioning/
formatting disks, writing raw data to a disk device, and fork bombs.

Relative paths are resolved in the project folder; absolute paths work anywhere.
Every tool returns text. Failures are returned as "ERROR: …" (never raised), so the model
can read what went wrong and adapt. Long output is shortened (start + end kept).
"""
from __future__ import annotations

import html
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BLOCKED: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[A-Za-z]*\s+)*-[A-Za-z]*[rR][A-Za-z]*\s+(-[A-Za-z]+\s+)*"
                r"(/|~/?|\$HOME/?|/\*|~/\*)(\s|;|&|\||$)"), "deleting / or your home folder"),
    (re.compile(r"\bdiskutil\s+(erase\w*|partition\w*|zero\w*|secureErase|reformat|apfs\s+delete\w*)", re.I),
     "erasing or partitioning a disk"),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bnewfs_\w+"), "formatting a disk"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(r?disk|sd|nvme)"), "writing raw data to a disk device"),
    (re.compile(r">\s*/dev/r?disk\d"), "writing raw data to a disk device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "a fork bomb"),
]

SKIP_DIRS = {".git", ".local-agent", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store"}


def blocked(command: str) -> str | None:
    for rx, why in BLOCKED:
        if rx.search(command):
            return why
    return None


def cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n…[{len(text) - limit:,} characters cut]…\n{text[-tail:]}"


@dataclass
class ToolCtx:
    project: Path
    cfg: dict = field(default_factory=dict)
    touch: Callable[[], None] = lambda: None          # "still making progress" (watchdog)
    check: Callable[[], None] = lambda: None          # raises if the job was cancelled
    searxng_port: int = 8888
    store: Any = None                                 # law.runtime.store.Store — durable memory
    telegram_chat_id: int | None = None               # set when the task came from Telegram
    paths: Any = None                                 # law.paths.Paths — for Telegram creds, etc.

    @property
    def limit(self) -> int:
        return int(self.cfg.get("tool_output_chars", 20000))

    def path(self, p: str) -> Path:
        q = Path(os.path.expanduser(str(p)))
        return q if q.is_absolute() else (self.project / q)


# ── tools ───────────────────────────────────────────────────────────────
def run_shell(ctx: ToolCtx, command: str, timeout_s: float | None = None) -> str:
    why = blocked(command)
    if why:
        return (f"REFUSED: this command looks like {why}. It is on law's hard-block list (no snapshot "
                "could undo it) and was NOT run. Use a narrower command.")
    limit_s = float(timeout_s or ctx.cfg.get("shell_timeout_s", 1800))
    env = {**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "/usr/bin:/bin"),
           "PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb", "CI": "1"}
    p = subprocess.Popen(["/bin/bash", "-lc", command], cwd=ctx.project, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace",
                         start_new_session=True, env=env)
    chunks: list[str] = []
    reader = threading.Thread(target=lambda: chunks.extend(iter(p.stdout.readline, "")), daemon=True)
    reader.start()
    t0 = time.time()
    timed_out = False
    try:
        while p.poll() is None:
            ctx.touch()
            ctx.check()
            if time.time() - t0 > limit_s:
                timed_out = True
                break
            time.sleep(0.25)
    finally:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            p.wait(10)
        reader.join(5)
    out = "".join(chunks)
    status = f"timed out after {limit_s:.0f}s (killed)" if timed_out else f"exit code {p.returncode}"
    return f"[{status}, {time.time() - t0:.1f}s]\n" + (cap(out, ctx.limit) if out.strip() else "(no output)")


def read_file(ctx: ToolCtx, path: str, start_line: int = 1, max_lines: int | None = None) -> str:
    f = ctx.path(path)
    if not f.exists():
        return f"ERROR: {f} does not exist"
    if f.is_dir():
        return f"ERROR: {f} is a folder — use list_dir"
    raw = f.read_bytes()
    if b"\0" in raw[:8000]:
        return f"ERROR: {f} looks like a binary file ({len(raw):,} bytes)"
    lines = raw.decode(errors="replace").splitlines()
    start = max(1, int(start_line))
    end = len(lines) if not max_lines else min(len(lines), start - 1 + int(max_lines))
    body = "\n".join(lines[start - 1:end])
    note = f"[{f} · lines {start}-{end} of {len(lines)}]\n"
    return note + cap(body, ctx.limit)


def write_file(ctx: ToolCtx, path: str, content: str) -> str:
    f = ctx.path(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    existed = f.exists()
    f.write_text(content)
    return f"{'overwrote' if existed else 'created'} {f} ({len(content):,} characters)"


def edit_file(ctx: ToolCtx, path: str, old: str, new: str, replace_all: bool = False) -> str:
    f = ctx.path(path)
    if not f.exists():
        return f"ERROR: {f} does not exist (use write_file to create it)"
    text = f.read_text(errors="replace")
    n = text.count(old) if old else 0
    if n == 0:
        return f"ERROR: the text to replace was not found in {f}. read_file it first and copy the text exactly."
    if n > 1 and not replace_all:
        return (f"ERROR: the text to replace appears {n} times in {f}. Include more surrounding text to make "
                "it unique, or set replace_all=true.")
    f.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
    return f"edited {f} ({n if replace_all else 1} replacement{'s' if (replace_all and n > 1) else ''})"


def list_dir(ctx: ToolCtx, path: str = ".", depth: int = 2) -> str:
    root = ctx.path(path)
    if not root.is_dir():
        return f"ERROR: {root} is not a folder"
    lines: list[str] = []

    def walk(d: Path, level: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            lines.append(f"{'  ' * level}[cannot read: {e}]")
            return
        for e in entries:
            if len(lines) >= 500:
                return
            if e.name in SKIP_DIRS:
                continue
            if e.is_dir():
                lines.append(f"{'  ' * level}{e.name}/")
                if level + 1 < depth:
                    walk(e, level + 1)
            else:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{'  ' * level}{e.name}  ({size:,} B)")

    walk(root, 0)
    more = "\n…(listing stopped at 500 entries)" if len(lines) >= 500 else ""
    return f"[{root}]\n" + ("\n".join(lines) or "(empty)") + more


def _http_get(url: str, timeout: float = 30, max_bytes: int = 3_000_000) -> tuple[str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (law local agent)",
                                               "Accept": "text/html,application/json,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Content-Type", ""), r.read(max_bytes)


def web_search(ctx: ToolCtx, query: str, max_results: int = 8) -> str:
    url = f"http://127.0.0.1:{ctx.searxng_port}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "safesearch": 0})
    try:
        _, raw = _http_get(url, timeout=30)
        data = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return (f"ERROR: web search failed — is SearXNG running on port {ctx.searxng_port}? "
                f"(`law status`) ({e})")
    results = data.get("results") or []
    if not results:
        return f"no results for: {query}"
    out = [f"{i}. {r.get('title', '').strip()}\n   {r.get('url', '')}\n   {(r.get('content') or '').strip()[:300]}"
           for i, r in enumerate(results[:int(max_results)], 1)]
    return cap("\n".join(out), ctx.limit)


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def web_fetch(ctx: ToolCtx, url: str) -> str:
    if not re.match(r"https?://", url or ""):
        return "ERROR: url must start with http:// or https://"
    try:
        ctype, raw = _http_get(url, timeout=45)
    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} for {url}"
    except (urllib.error.URLError, OSError) as e:
        return f"ERROR: could not fetch {url} ({e})"
    text = raw.decode(errors="replace")
    if "html" in ctype.lower() or "<html" in text[:2000].lower():
        text = html_to_text(text)
    return f"[{url}]\n" + cap(text, ctx.limit)


def remember(ctx: ToolCtx, fact: str) -> str:
    if ctx.store is None:
        return "ERROR: no memory store available"
    fact = " ".join(str(fact).split())
    if not fact:
        return "ERROR: remember needs a non-empty fact"
    added = ctx.store.add_memory(ctx.project, fact)
    return "remembered" if added else "already remembered"


SEARCH_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml",
              ".html", ".css", ".scss", ".sh", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp",
              ".rb", ".php", ".sql", ".ini", ".cfg", ".conf", ".env", ".vue", ".svelte", ".xml",
              ".gradle", ".kt", ".swift", ".m", ".mm", ".dockerfile", ".tf", ".proto", ".graphql"}


def search_files(ctx: ToolCtx, pattern: str, path: str = ".", glob: str | None = None,
                 ignore_case: bool = False, max_results: int = 60) -> str:
    """Find where a regex/keyword appears across the project's text files — returns file:line: match,
    skipping junk dirs and binaries. Keyword search (no embeddings), so it never needs a model and is
    safe to call mid-run; for meaning-based search use the dashboard/CLI RAG."""
    if not str(pattern).strip():
        return "ERROR: search_files needs a non-empty pattern"
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"ERROR: invalid regex pattern: {e}"
    root = ctx.path(path)
    if not root.exists():
        return f"ERROR: {root} does not exist"
    files = [root] if root.is_file() else sorted(root.rglob(glob) if glob else root.rglob("*"))
    cap_n = max(1, min(int(max_results), 500))
    hits: list[str] = []
    scanned = 0
    for f in files:
        if len(hits) >= cap_n:
            break
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if not f.is_file():
            continue
        if root.is_dir() and not glob and f.suffix.lower() not in SEARCH_EXT:
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            raw = f.read_bytes()
            if b"\0" in raw[:8000]:
                continue
            ctx.check()
            ctx.touch()
            scanned += 1
            rel = f.relative_to(root) if (root.is_dir() and f.is_relative_to(root)) else f
            for i, line in enumerate(raw.decode(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= cap_n:
                        break
        except OSError:
            continue
    if not hits:
        return f"no matches for /{pattern}/ in {root} ({scanned} files scanned)"
    more = f"\n…(stopped at {cap_n} matches)" if len(hits) >= cap_n else ""
    return f"[{len(hits)} match{'es' if len(hits) != 1 else ''} in {root}]\n" + cap("\n".join(hits), ctx.limit) + more


def send_email(ctx: ToolCtx, to: str, subject: str, body: str = "", attachment: str = "") -> str:
    """Send an email via the macOS Mail app (a configured account must exist). Reliable and NON-blocking:
    it composes an invisible message, attaches a file if given, sends, and gives up after a timeout — so
    it never hangs the way hand-written Mail AppleScript does. Use this instead of osascript for email."""
    to = (to or "").strip()
    if "@" not in to:
        return f"ERROR: '{to}' is not a valid email address."
    att = ""
    if attachment.strip():
        p = ctx.path(attachment)
        if not p.is_file():
            return f"ERROR: attachment {p} does not exist."
        att = str(p)

    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')
    lines = ['tell application "Mail"',
             f'set m to make new outgoing message with properties {{subject:"{esc(subject)}", '
             f'content:"{esc(body)}", visible:false}}',
             'tell m',
             f'make new to recipient at end of to recipients with properties {{address:"{esc(to)}"}}']
    if att:
        lines.append(f'make new attachment with properties {{file name:(POSIX file "{esc(att)}")}} '
                     'at after the last paragraph')
        lines.append('delay 1')
    lines += ['send', 'end tell', 'end tell', 'return "ok"']
    cmd = ["osascript"] + sum([["-e", ln] for ln in lines], [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return ("ERROR: Mail timed out (90s). It may need a one-time Automation permission — the user must "
                "approve 'control Mail' in System Settings > Privacy & Security > Automation, then retry.")
    ctx.touch()
    if r.returncode != 0:
        return f"ERROR: sending failed: {(r.stderr or r.stdout or '').strip()[:300]}"
    # verify it actually left (Mail queues in the Outbox, then moves to Sent)
    time.sleep(2)
    chk = subprocess.run(["osascript", "-e",
                          f'tell application "Mail" to get count of (messages of sent mailbox whose subject is "{esc(subject)}")'],
                         capture_output=True, text=True, timeout=20)
    sent = (chk.stdout or "0").strip()
    return (f"Email sent to {to}" + (f" with attachment {Path(att).name}" if att else "") +
            (f" (confirmed in Sent: {sent})." if sent not in ("0", "") else " (queued; check Sent to confirm delivery)."))


def look_at(ctx: ToolCtx, image: str, question: str = "") -> str:
    """Look at an image (a screenshot you took, a photo, a design) with the vision model and get back a
    description / assessment. This is how you SEE: screenshot a running web page, then look_at it to judge
    the real UI and list flaws. Runs the VLM out-of-process, so it works even mid-agent-run."""
    from ..runtime import visiongen
    p = ctx.path(image)
    if not p.is_file():
        return f"ERROR: {p} is not a file (take a screenshot first, then look_at it)."
    if ctx.paths is None or not visiongen.available(ctx.paths):
        return "ERROR: the vision model is not available on this machine."
    q = question.strip() or ("Describe this screenshot in detail. If it is a web UI, assess the layout, "
                             "spacing, typography, colour and overall quality, and list every visible "
                             "problem (broken images, unstyled/overlapping elements, empty areas).")
    try:
        res = visiongen.describe(ctx.paths, p, q, touch=ctx.touch, check=ctx.check)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in ("Cancelled", "Interrupted"):
            raise
        return f"ERROR: look_at failed: {type(e).__name__}: {e}"
    return f"[looked at {p.name}]\n" + (res.get("text") or "(no description)")


def send_file(ctx: ToolCtx, path: str, caption: str = "") -> str:
    """Send a local file to the user over Telegram — a generated image, audio, a video, or any
    document/report. Pick the right file and a short caption; it arrives as a photo/audio/video/
    file in their chat. Only works when the task was started from Telegram."""
    cid = ctx.telegram_chat_id
    if not cid:
        return ("ERROR: send_file only works for tasks started from Telegram (there is no chat to "
                "send to). The file is still saved on disk for the user.")
    f = ctx.path(path)
    if not f.is_file():
        return f"ERROR: {f} is not a file (nothing to send)."
    from ..paths import get_paths
    from ..telegram import Api, creds, media_kind
    token, _ = creds(ctx.paths or get_paths())
    if not token:
        return "ERROR: Telegram is not configured on this machine (no bot token)."
    api = Api(token)
    method, field = media_kind(f)
    params = {"chat_id": cid}
    if str(caption).strip():
        params["caption"] = str(caption)[:1024]
    for m, fld in ((method, field), ("sendDocument", "document")):
        try:
            api.upload(m, fld, f, **params)
            return f"sent {f.name} ({f.stat().st_size:,} bytes) to the user on Telegram."
        except Exception as e:  # noqa: BLE001 — try a plain document, then report
            last = e
            if m == "sendDocument":
                break
    return f"ERROR: could not send {f.name} to Telegram: {type(last).__name__}: {last}"


CHROME_PATHS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]


def _chrome() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    for name in ("chromium", "chromium-browser", "google-chrome"):
        import shutil
        if shutil.which(name):
            return name
    return None


def screenshot(ctx: ToolCtx, url: str, out: str | None = None, width: int = 1366, height: int = 900) -> str:
    """Render a URL in a headless browser and save a PNG, so a web result can be LOOKED AT (by the
    product owner / designer / a human) instead of trusting an HTTP 200. Returns the saved path."""
    chrome = _chrome()
    if not chrome:
        return ("ERROR: no headless browser found (install Google Chrome). You can still verify the "
                "page by fetching its HTML with run_shell `curl` and checking the content and styles.")
    dest = ctx.path(out) if out else (ctx.project / ".screenshots" / f"shot-{int(time.time())}.png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
                            f"--window-size={int(width)},{int(height)}", "--virtual-time-budget=5000",
                            f"--screenshot={dest}", url],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return f"ERROR: the browser timed out rendering {url} (is the app actually up on that port?)"
    ctx.touch()
    if not dest.exists() or dest.stat().st_size == 0:
        return f"ERROR: could not capture {url}. Browser said: {(r.stderr or r.stdout or '')[:300]}"
    kb = dest.stat().st_size / 1024
    # A PNG of a mostly-blank page compresses tiny; a real, content-rich page is much larger. This is a
    # keyless signal so even an agent that can't SEE the image knows when a page rendered blank/empty.
    blank = kb < 14
    flag = (f"\n⚠️ WARNING: this capture is only {kb:.0f} KB, which usually means the page rendered BLANK or "
            f"nearly empty (no images/content loaded). Treat this as a FAILURE: check the browser console / "
            f"the served HTML, fix the cause, and screenshot again." if blank else
            f"\nThe capture is {kb:.0f} KB (looks content-rich). Still open it to judge the actual layout/quality.")
    return (f"Saved screenshot of {url} to {dest} ({kb:.0f} KB, {width}x{height})." + flag)


FUNCS: dict[str, Callable[..., str]] = {
    "run_shell": run_shell, "read_file": read_file, "write_file": write_file, "edit_file": edit_file,
    "list_dir": list_dir, "search_files": search_files, "web_search": web_search,
    "web_fetch": web_fetch, "remember": remember, "screenshot": screenshot, "send_file": send_file,
    "look_at": look_at, "send_email": send_email,
}


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
SCHEMAS = [
    _fn("run_shell", "Run a shell command (bash) in the project folder and return its output and exit "
        "code. Use it to run programs, tests, git, docker, brew, curl, etc.",
        {"command": S, "timeout_s": {"type": "number", "description": "optional time limit in seconds"}},
        ["command"]),
    _fn("read_file", "Read a text file. Relative paths are inside the project folder.",
        {"path": S, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}}, ["path"]),
    _fn("write_file", "Create or completely overwrite a file with the given content.",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("edit_file", "Replace an exact piece of text in a file. `old` must match exactly once "
        "(unless replace_all is true). Prefer this over rewriting whole files.",
        {"path": S, "old": S, "new": S, "replace_all": {"type": "boolean"}}, ["path", "old", "new"]),
    _fn("list_dir", "List files and folders (default: the project folder, 2 levels deep).",
        {"path": S, "depth": {"type": "integer"}}, []),
    _fn("search_files", "Find where a regex/keyword appears across the project's text files. Returns "
        "`file:line: matching text`, skipping junk dirs and binaries. Fast keyword search — use it to "
        "locate code/config before reading whole files.",
        {"pattern": S, "path": {"type": "string", "description": "folder or file to search (default: project)"},
         "glob": {"type": "string", "description": "optional filename glob, e.g. '*.py' or '*.tsx'"},
         "ignore_case": {"type": "boolean"}, "max_results": {"type": "integer"}}, ["pattern"]),
    _fn("web_search", "Search the web (private SearXNG). Returns titles, URLs and snippets.",
        {"query": S, "max_results": {"type": "integer"}}, ["query"]),
    _fn("web_fetch", "Download a web page and return it as plain text.", {"url": S}, ["url"]),
    _fn("screenshot", "Render a URL in a headless browser and save a PNG image of the page, so a web "
        "result can be visually assessed (layout/styling), not just checked for HTTP 200. Use it on a "
        "running web app (e.g. http://localhost:3006), then look_at the saved PNG to actually see it.",
        {"url": S, "out": {"type": "string", "description": "optional output path for the PNG"},
         "width": {"type": "integer"}, "height": {"type": "integer"}}, ["url"]),
    _fn("send_email", "Send an email via the macOS Mail app (reliable, non-blocking — use this instead of "
        "hand-written osascript, which hangs). Give the recipient, subject, body, and an optional file to "
        "attach. Needs a configured Mail account + one-time Automation permission.",
        {"to": S, "subject": S, "body": {"type": "string", "description": "email body text"},
         "attachment": {"type": "string", "description": "optional path to a file to attach"}}, ["to", "subject"]),
    _fn("look_at", "SEE an image with the vision model: pass a screenshot/photo/design and get a detailed "
        "description and UI assessment back. The way to judge a web app you built — screenshot the running "
        "page, then look_at it and fix the flaws it reports. Works mid-run.",
        {"image": S, "question": {"type": "string", "description": "what to assess (optional)"}}, ["image"]),
    _fn("remember", "Save one short, durable fact about this project or the user to project memory, so "
        "future agent runs know it.", {"fact": S}, ["fact"]),
    _fn("send_file", "Send a local file to the user over Telegram — a generated image, an audio clip, a "
        "video, or any document/report. It arrives as a photo/audio/video/file in their chat. Use it "
        "whenever the user asked you to produce something and send it. Only works for Telegram tasks.",
        {"path": S, "caption": {"type": "string", "description": "optional short caption"}}, ["path"]),
]
DELEGATE_SCHEMA = _fn(
    "delegate", "Hand a self-contained sub-task to another persona. It runs as its own job after this "
    "step; you continue when it has finished and you receive its report. Write the task so it can be "
    "done without seeing this conversation (goal, files, constraints, how to verify).",
    {"persona": {"type": "string", "description": "e.g. coder, qa, designer, po, writer, researcher"},
     "task": S}, ["persona", "task"])


def execute(name: str, args: dict, ctx: ToolCtx) -> str:
    fn = FUNCS.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(FUNCS)}"
    if not isinstance(args, dict):
        return "ERROR: tool arguments must be a JSON object"
    try:
        return fn(ctx, **args)
    except TypeError as e:
        return f"ERROR: wrong arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001 — tools never crash the agent
        if type(e).__name__ in ("Cancelled", "Interrupted"):
            raise
        return f"ERROR: {name} failed: {type(e).__name__}: {e}"
