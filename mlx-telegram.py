#!/usr/bin/env python3
"""
mlx-telegram — Telegram bridge for the local MLX agent.

Puts the whole persona/agent stack in your pocket:
  • Live "thinking then collapse": one message updates in place with the agent's
    tool trace while it works, then collapses to just the final answer.
  • Native inline-button approvals: the [y]/[n]/[a] gate becomes tappable Yes / No /
    Allow-rest-of-run buttons; the agent's ask_choice becomes a row of buttons.
  • Locked to ONE Telegram user (your numeric ID) — ignores everyone else.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID from ~/.mlx-ai-workstation/.env
(set them with ./mlx-setup.sh --configure). Uses only `requests` — no bot SDK,
so nothing to version-clash with the rest of the stack.

Run:  ./mlx-setup.sh --telegram      (or as a launchd service; see the installer)
"""
from __future__ import annotations
import importlib.util, os, sys, threading, time, re
import html as _html
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests missing — run under the workstation venv.")

WORKDIR = Path(os.environ.get("MLX_WORKDIR", str(Path.home() / ".mlx-ai-workstation")))
ENV_FILE = WORKDIR / ".env"
AGENT_PATH = WORKDIR / "agent" / "mlx-agent.py"

# ── load .env (KEY=VALUE lines) ────────────────────────────────────────────
def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip()
    return env

ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_ID = str(ENV.get("TELEGRAM_USER_ID") or os.environ.get("TELEGRAM_USER_ID", "")).strip()
if not TOKEN or not USER_ID:
    sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID in .env — run ./mlx-setup.sh --configure")
API = f"https://api.telegram.org/bot{TOKEN}"

# ── load the agent as a module and point it at our workspace ───────────────
spec = importlib.util.spec_from_file_location("mlx_agent", str(AGENT_PATH))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

# ── Telegram API helpers (raw HTTP) ────────────────────────────────────────
def tg(method: str, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=65)
        if r.status_code == 429:                       # rate limited — back off, don't crash
            try: time.sleep(min(int(r.json().get("parameters", {}).get("retry_after", 1)), 5))
            except Exception: time.sleep(1)
            return {"ok": False, "retry_after": True}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

def kb(rows):   # rows: list[list[(label, data)]] → InlineKeyboardMarkup
    return {"inline_keyboard": [[{"text": l, "callback_data": d} for (l, d) in row] for row in rows]}

def _plainish(html_text: str) -> str:
    # strip our tags + unescape entities, for the plain-text fallback
    return _html.unescape(re.sub(r"<[^>]+>", "", html_text))

def send(text: str, buttons=None, parse_mode=None):
    p = {"chat_id": USER_ID, "text": (text or "…")[:4096], "disable_web_page_preview": True}
    if buttons is not None: p["reply_markup"] = buttons
    if parse_mode: p["parse_mode"] = parse_mode
    r = tg("sendMessage", **p)
    if parse_mode and not r.get("ok"):                 # formatting rejected → send plain, never lose the msg
        p.pop("parse_mode", None); p["text"] = _plainish(text)[:4096]; r = tg("sendMessage", **p)
    return r.get("result", {}).get("message_id")

def edit(mid, text: str, buttons=None, parse_mode=None):
    p = {"chat_id": USER_ID, "message_id": mid, "text": (text or "…")[:4096], "disable_web_page_preview": True}
    if buttons is not None: p["reply_markup"] = buttons
    if parse_mode: p["parse_mode"] = parse_mode
    r = tg("editMessageText", **p)
    # retry plain ONLY on a real parse failure — "not modified" is not an error
    if parse_mode and not r.get("ok") and "not modified" not in str(r).lower():
        p.pop("parse_mode", None); p["text"] = _plainish(text)[:4096]; r = tg("editMessageText", **p)
    return r

def _chunks(text: str, limit: int = 3800):
    """Split text into <=limit pieces at line boundaries (hard-splitting any overlong line),
    so a long answer can flow across several Telegram messages instead of being cut at 4096."""
    text = text or ""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur: out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur); cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur: out.append(cur)
    return out or [""]

def answer_cb(cb_id, text=""):
    tg("answerCallbackQuery", callback_query_id=cb_id, text=text)

def typing():
    # Shows Telegram's "typing…" bubble (~5s). Re-sent periodically while the agent
    # works so the chat looks alive even during long model generation with no tool calls.
    tg("sendChatAction", chat_id=USER_ID, action="typing")

# ── HTML formatting (Telegram-safe) ────────────────────────────────────────
def esc(s: str) -> str:
    return _html.escape(s or "", quote=False)   # escapes & < >

def _inline(seg: str) -> str:
    """Escaped text → apply inline code, bold, and header styling."""
    seg = esc(seg)
    seg = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", seg)          # `code`
    seg = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", seg)          # **bold**
    lines = seg.split("\n")
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*#{1,6}\s+(.*\S)\s*$", ln)               # markdown header → bold
        if m:
            lines[i] = f"<b>{m.group(1)}</b>"; continue
        # a short line ending in ':' reads as a section header → bold it
        s = ln.strip()
        if s and s.endswith(":") and len(s) <= 48 and "<code>" not in s:
            lines[i] = f"<b>{s}</b>"
    return "\n".join(lines)

def _tables_to_code(text: str) -> str:
    """Telegram HTML has no <table>. Convert Markdown pipe-tables into an aligned
       monospace block (wrapped as a fenced code block so to_html renders it in <pre>,
       where columns line up). Non-table text is untouched."""
    lines = (text or "").split("\n"); out = []; i = 0
    sep = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
    def cells(row): return [c.strip() for c in row.strip().strip("|").split("|")]
    while i < len(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "|" in lines[i] and "|" in nxt and "-" in nxt and sep.match(nxt):
            header = cells(lines[i]); j = i + 2; data = []
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                data.append(cells(lines[j])); j += 1
            rows = [header] + data
            nc = max(len(r) for r in rows)
            rows = [r + [""] * (nc - len(r)) for r in rows]
            w = [max(len(rows[k][c]) for k in range(len(rows))) for c in range(nc)]
            fmt = lambda r: "  ".join(r[c].ljust(w[c]) for c in range(nc)).rstrip()
            tbl = [fmt(rows[0]), "  ".join("-" * w[c] for c in range(nc))] + [fmt(r) for r in rows[1:]]
            out.append("```\n" + "\n".join(tbl) + "\n```")
            i = j
        else:
            out.append(lines[i]); i += 1
    return "\n".join(out)

def to_html(text: str) -> str:
    """Convert the model's lightweight-markdown answer to Telegram-safe HTML:
       Markdown tables → aligned monospace, fenced ``` → <pre>, inline `x` → <code>,
       **x**/headers → bold."""
    text = _tables_to_code(text or "")
    out, parts = [], re.split(r"```[ \t]*\w*\n?(.*?)```", text, flags=re.DOTALL)
    for i, seg in enumerate(parts):
        out.append(f"<pre>{esc(seg.rstrip())}</pre>" if i % 2 else _inline(seg))
    return "".join(out)

def split_thinking(raw: str):
    """Separate <think>…</think> reasoning from the visible answer — WITHOUT deleting
       it. Returns (visible_answer, thinking). Handles an unclosed <think> (model still
       mid-thought) by treating the trailing text as thinking."""
    raw = raw or ""
    thinks = re.findall(r"<think>(.*?)</think>", raw, flags=re.DOTALL | re.IGNORECASE)
    visible = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    m = re.search(r"<think>(.*)$", visible, flags=re.DOTALL | re.IGNORECASE)   # unclosed tag
    if m:
        thinks.append(m.group(1)); visible = visible[:m.start()]
    return visible.strip(), "\n\n".join(t.strip() for t in thinks if t.strip()).strip()

# ── shared state (single user → one run + one pending gate at a time) ───────
STATE = {
    "persona": "orchestrator",     # default main agent
    "busy": False,
    "pending": None,               # {"event":Event, "result":..., "kind":"approval|choice"}
    "history": [],                 # rolling [{role,content}] so follow-ups keep context
}
LOCK = threading.Lock()

# ── live "thinking" message that updates in place, then collapses ──────────
class Live:
    def __init__(self, persona):
        self.persona = persona
        self.buf = []
        self.start = time.time()
        self.msg_id = send(f"🧠 {persona} is working…")
        self.lk = threading.Lock()
        self._last_text = ""
        self.done = False        # once collapsed, no keeper render may overwrite the answer
        self._tick = 0           # drives a visible spinner so it's obvious the agent is alive
        self.answer = ""         # streamed final-answer text (grows token by token)

    def emit(self, line: str):
        # CHEAP: just buffer the line. No API call here — a single renderer thread
        # does all the editing at a steady, rate-safe cadence. This is why heavy
        # tool activity can't trip Telegram's per-message edit limit anymore.
        with self.lk:
            self.buf.append(line.rstrip())
            self.answer = ""     # a tool step means we're not in the final-answer phase yet

    def stream(self, piece: str):
        # A token/delta of the final answer. Cheap append; the keeper renders it on
        # its throttled cadence (so token streaming can't trip Telegram's rate limit).
        with self.lk:
            self.answer += piece

    def render(self):
        # Called on a fixed cadence by the keeper. Safe: skips if text is unchanged
        # (avoids Telegram "message is not modified"), and never raises.
        with self.lk:
            if self.done:      # already collapsed to the answer — never overwrite it
                return
            self._tick += 1
            spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._tick % 10]
            secs = int(time.time() - self.start)
            ans = self.answer
            body = "\n".join(self.buf[-24:])[-3500:]
        if ans:                # streaming the answer — show it growing, formatted
            vis, think = split_thinking(ans)
            shown = to_html(vis) if vis else ("💭 <i>thinking…</i>" if think else "…")
            text = f"✍️ <b>{esc(self.persona)}</b> {spin} {secs}s\n{shown}"
        else:                  # still working through tools — show the live trace
            header = f"🧠 <b>{esc(self.persona)}</b> working… {spin} {secs}s"
            text = header + (f"\n<pre>{esc(body)}</pre>" if body else "")
        text = text[:4096]
        if text == self._last_text:
            return
        self._last_text = text
        try: edit(self.msg_id, text, parse_mode="HTML")
        except Exception: pass

    def collapse(self, raw: str, steps: int):
        with self.lk:
            self.done = True                       # from here on, render() is a no-op
        visible, thinking = split_thinking(raw)
        foot = (f"\n\n<i>· {esc(self.persona)} · {steps} steps · {int(time.time()-self.start)}s</i>"
                if steps else "")
        if not (visible or "").strip():
            # model produced only reasoning → don't show an empty answer
            head = "<i>(reasoning only — see below)</i>" if thinking else "<i>(no output)</i>"
            self._last_text = (head + foot)[:4096]
            edit(self.msg_id, self._last_text, parse_mode="HTML")
        else:
            # Split the FULL answer into <=Telegram-limit pieces so nothing is truncated.
            # First piece edits the live message; the rest are sent as follow-up messages.
            parts = _chunks(visible, 3800)
            first = to_html(parts[0]).strip() + (foot if len(parts) == 1 else "")
            self._last_text = first[:4096]
            r = edit(self.msg_id, self._last_text, parse_mode="HTML")
            if not r.get("ok") and "not modified" not in str(r).lower():
                edit(self.msg_id, _plainish(parts[0])[:4096])
            for i, part in enumerate(parts[1:], start=1):
                tail = foot if i == len(parts) - 1 else ""
                send(to_html(part).strip() + tail, parse_mode="HTML")
        if thinking:                               # reasoning as its own (single) message
            tp = thinking.strip()
            send("💭 <b>thinking</b>\n<pre>" + esc(tp[:3800]) + ("…" if len(tp) > 3800 else "") + "</pre>",
                 parse_mode="HTML")

# ── approval + choice handlers injected into the agent ─────────────────────
def _wait_gate(kind: str, timeout=600):
    ev = threading.Event()
    with LOCK:
        STATE["pending"] = {"event": ev, "result": None, "kind": kind}
    if not ev.wait(timeout=timeout):            # user didn't respond in time → safe default
        with LOCK: STATE["pending"] = None
        return None
    with LOCK:
        res = STATE["pending"]["result"] if STATE["pending"] else None
        STATE["pending"] = None
    return res

def ask_fn(prompt: str, allow_all: bool) -> str:
    rows = [[("✅ Yes", "gate:y"), ("❌ No", "gate:n")]]
    if allow_all: rows.append([("⏩ Allow rest of this run", "gate:a")])
    send(f"⚠️ Approve this action?\n\n{prompt[:900]}", buttons=kb(rows))
    res = _wait_gate("approval")
    return res if res in ("y", "n", "a") else "n"

def choice_fn(question: str, options: list) -> str:
    opts = [str(o) for o in options][:8]
    rows = [[(o[:40], f"choice:{i}")] for i, o in enumerate(opts)]
    send(f"❓ {question[:900]}", buttons=kb(rows))
    res = _wait_gate("choice")
    try: return opts[int(res)] if res is not None else opts[0]
    except Exception: return opts[0]

# ── run one task on a worker thread (keeps the poll loop free for callbacks) ─
def run_task(text: str):
    persona_name = STATE["persona"]
    personas = agent.load_personas()
    persona = personas.get(persona_name) or personas.get("general")
    live = Live(persona_name)
    # ONE controlled updater for the whole task: on a steady 2s cadence it keeps the
    # "typing…" bubble alive and re-renders the live message. Because emit() only
    # buffers (no API call), edits happen at this fixed rate no matter how many tools
    # fire — so tool-heavy tasks can't trip Telegram's rate limit. Fully wrapped so a
    # transient API error can never kill the loop (the old failure mode).
    stop_keeper = threading.Event()
    def _keeper():
        while not stop_keeper.is_set():
            try:
                typing()
                live.render()
            except Exception as e:
                print("keeper (non-fatal):", e)
            stop_keeper.wait(2.0)
    keeper = threading.Thread(target=_keeper, daemon=True)
    keeper.start()
    # wire the agent's hooks to Telegram for the duration of this run
    agent.EMIT_FN = live.emit
    agent.STREAM_FN = live.stream     # stream the final answer token-by-token (rate-safe via the keeper)
    agent.ASK_FN = ask_fn
    agent.CHOICE_FN = choice_fn
    try:
        history = list(STATE.get("history") or [])
        answer = agent.run_agent(text, persona, history=history)
    except Exception as e:
        answer = f"⚠️ error: {e}"
    finally:
        stop_keeper.set()
        keeper.join(timeout=5)     # wait out any in-flight render BEFORE we write the answer
        agent.EMIT_FN = agent.ASK_FN = agent.CHOICE_FN = None
        agent.STREAM_FN = None
    live.collapse(answer, len(live.buf))     # collapse now preserves thinking itself
    with LOCK:
        # remember this turn (visible answer only, thinking stripped) so a follow-up
        # like "and who came second?" resolves against the topic, not a cold start
        vis, _ = split_thinking(answer)
        hist = STATE.get("history") or []
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": (vis or answer)})
        STATE["history"] = hist[-20:]        # keep the last ~10 exchanges
        STATE["busy"] = False

# ── command handling ───────────────────────────────────────────────────────
HELP = ("🤖 Local MLX agent\n\n"
        "Just send a task and I'll work on it — you'll see live progress, and I'll ask "
        "with buttons before anything risky.\n\n"
        "/personas — list personas\n"
        "/use <name> — switch persona (current: {p})\n"
        "/reset — start a fresh conversation (forget the current thread)\n"
        "/help — this message")

def handle_text(text: str):
    if text.startswith("/"):
        cmd, *rest = text[1:].split(maxsplit=1)
        arg = rest[0].strip() if rest else ""
        if cmd in ("start", "help"):
            send(HELP.format(p=STATE["persona"])); return
        if cmd in ("reset", "new", "clear"):
            with LOCK: STATE["history"] = []
            send("🧹 Fresh start — I've cleared the conversation."); return
        if cmd == "personas":
            p = agent.load_personas()
            lines = [f"• {n}{'  ← active' if n==STATE['persona'] else ''}\n   {c.get('description','')}"
                     for n, c in p.items()]
            send("Personas:\n\n" + "\n".join(lines)); return
        if cmd == "use":
            p = agent.load_personas()
            if arg in p:
                STATE["persona"] = arg
                with LOCK: STATE["history"] = []      # new specialist → fresh thread
                send(f"✅ now using: {arg}  (conversation reset)")
            else: send(f"No persona '{arg}'. See /personas.")
            return
        send("Unknown command. /help"); return
    # a task
    with LOCK:
        if STATE["busy"]:
            send("⏳ Still working on the previous task — one at a time. Send it again when I'm done.")
            return
        STATE["busy"] = True
    typing()   # instant feedback — the "working…" message + live spinner follow within a second
    threading.Thread(target=run_task, args=(text,), daemon=True).start()

def _tapped_label(cb, data):
    for row in ((cb.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard", []):
        for btn in row:
            if btn.get("callback_data") == data:
                return btn.get("text", "")
    return ""

def handle_callback(cb):
    data = cb.get("data", "")
    m = cb.get("message") or {}
    mid, orig = m.get("message_id"), m.get("text", "")
    label = _tapped_label(cb, data)
    with LOCK:
        pend = STATE["pending"]
    if not pend:
        answer_cb(cb["id"], "nothing pending")
        if mid: edit(mid, (orig + "\n\n⏱ expired")[:4096])   # drop stale buttons too
        return
    if data.startswith("gate:") and pend["kind"] == "approval":
        pend["result"] = data.split(":", 1)[1]
        note = {"y": "✅ Approved", "n": "❌ Denied", "a": "⏩ Approved (rest of run)"}.get(pend["result"], label)
        answer_cb(cb["id"], note)
        if mid: edit(mid, (orig + f"\n\n→ {note}")[:4096])    # no buttons passed ⇒ keyboard removed
        pend["event"].set()
    elif data.startswith("choice:") and pend["kind"] == "choice":
        pend["result"] = data.split(":", 1)[1]
        answer_cb(cb["id"], "got it")
        if mid: edit(mid, (orig + f"\n\n→ {label or 'selected'}")[:4096])
        pend["event"].set()
    else:
        answer_cb(cb["id"])
        if mid: edit(mid, (orig + "\n\n(dismissed)")[:4096])

# ── long-poll loop ─────────────────────────────────────────────────────────
def main():
    me = tg("getMe")
    if not me.get("ok"):
        sys.exit(f"Telegram auth failed — check TELEGRAM_BOT_TOKEN. ({me})")
    print(f"mlx-telegram up as @{me['result'].get('username')} · locked to user {USER_ID}")
    send("🟢 Local MLX agent online. Send a task, or /help.")
    offset = None
    while True:
        r = tg("getUpdates", offset=offset, timeout=50)
        if not r.get("ok"):
            time.sleep(3); continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            msg = upd.get("message") or upd.get("edited_message")
            frm = (cb or msg or {}).get("from", {})
            if str(frm.get("id")) != USER_ID:          # hard user-ID lock
                if cb: answer_cb(cb["id"], "not authorized")
                continue
            try:
                if cb: handle_callback(cb)
                elif msg and "text" in msg: handle_text(msg["text"].strip())
            except Exception as e:
                print("handler error:", e)

if __name__ == "__main__":
    main()
