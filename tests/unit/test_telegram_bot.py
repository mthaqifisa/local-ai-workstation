"""Telegram adapter: only allow-listed users are served, and the bot refuses to start open."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from law import secrets, telegram_bot
from law.paths import LawPaths

REPO = Path(__file__).resolve().parents[2]
TOKEN = "123456:" + "A" * 35  # syntactically valid, never used against Telegram


def bot(allowed: set[int]) -> telegram_bot.LawBot:
    return telegram_bot.LawBot(TOKEN, allowed, "http://127.0.0.1:8100")


@pytest.mark.parametrize("user_id,ok", [(42, True), (7, True), (99, False)])
def test_allow_list(user_id: int, ok: bool):
    message = SimpleNamespace(from_user=SimpleNamespace(id=user_id))
    assert bot({42, 7})._ok(message) is ok  # type: ignore[arg-type]


def test_message_without_sender_is_rejected():
    assert bot({42})._ok(SimpleNamespace(from_user=None)) is False  # type: ignore[arg-type]


def _paths(sandbox: Path) -> LawPaths:
    return LawPaths(repo=REPO, home=sandbox / "LAW", user_home=sandbox)


def test_refuses_to_start_without_allow_list(sandbox: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    paths = _paths(sandbox)
    secrets.ensure(paths.env_file, imported={"LAW_TELEGRAM_TOKEN": TOKEN})
    started: list[bool] = []
    monkeypatch.setattr(telegram_bot.LawBot, "run", lambda self: started.append(True))
    telegram_bot.main(paths)
    assert started == []
    assert "refusing to start an open bot" in caplog.text


def test_does_nothing_without_token(sandbox: Path, caplog):
    telegram_bot.main(_paths(sandbox))
    assert "no LAW_TELEGRAM_TOKEN" in caplog.text


def test_split_message_keeps_everything_under_the_limit():
    text = "\n\n".join(f"Paragraph {i} " + "x" * 900 for i in range(10))
    parts = telegram_bot.split_message(text, limit=4000)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts).count("Paragraph") == 10


def test_split_message_short_text_is_one_part():
    assert telegram_bot.split_message("hello") == ["hello"]


def test_delivered_persists_across_restarts(tmp_path: Path):
    f = tmp_path / "delivered.txt"
    d = telegram_bot.Delivered(f)
    d.add("job1")
    assert telegram_bot.Delivered(f).seen("job1")
    assert not telegram_bot.Delivered(f).seen("job2")


async def test_resume_delivers_only_undelivered_recent_telegram_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import time as _t

    b = telegram_bot.LawBot(TOKEN, {42}, "http://api", delivered=tmp_path / "d.txt")
    b.delivered.add("old-done")
    now = _t.time()
    jobs = [
        {"id": "pending", "source": "telegram", "reply_to": {"chat_id": 1, "status_id": 2}, "parent": None, "created": now},
        {"id": "old-done", "source": "telegram", "reply_to": {"chat_id": 1, "status_id": 3}, "parent": None, "created": now},
        {"id": "stage", "source": "telegram", "reply_to": None, "parent": "pending", "created": now},
        {"id": "cli", "source": "cli", "reply_to": None, "parent": None, "created": now},
        {"id": "stale", "source": "telegram", "reply_to": {"chat_id": 1, "status_id": 4}, "parent": None,
         "created": now - 3 * 86400},
    ]

    class Resp:
        def json(self):  # type: ignore[no-untyped-def]
            return jobs

    class FakeClient:
        def __init__(self, *a, **k): ...  # type: ignore[no-untyped-def]
        async def __aenter__(self): return self  # type: ignore[no-untyped-def]
        async def __aexit__(self, *a): return None  # type: ignore[no-untyped-def]
        async def get(self, *a, **k): return Resp()  # type: ignore[no-untyped-def]

    delivered: list[str] = []

    async def fake_deliver(job_id, reply, again=False):  # type: ignore[no-untyped-def]
        delivered.append(job_id)

    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(b, "_deliver", fake_deliver)
    await b.resume()
    import asyncio as _a
    await _a.sleep(0)
    assert delivered == ["pending"]


def _job(**kw):  # type: ignore[no-untyped-def]
    import time as _t

    base = {"id": "abc123", "kind": "task", "status": "running", "source": "cli", "parent": None,
            "created": _t.time(), "payload": {"prompt": "Build LAN Chat", "role": "coder"}, "result": None, "error": None}
    return {**base, **kw}


def test_notice_started_shows_prompt_and_origin():
    text = telegram_bot.notice_started(_job())
    assert "Started" in text and "task (coder)" in text and "the law command" in text and "Build LAN Chat" in text


def test_notice_finished_shows_review_and_project():
    job = _job(status="done", result={"answer": "All gates green", "review": {"score": 3}, "fixed": True,
                                      "dir": "~/SourceCode/lan-chat"})
    text = telegram_bot.notice_finished(job)
    assert "reviewer 3/5 (fixed after review)" in text and "~/SourceCode/lan-chat" in text and "All gates green" in text
    assert "Failed" in telegram_bot.notice_finished(_job(status="failed", error="boom"))


async def test_watcher_reports_each_job_once_and_skips_other_sources(tmp_path: Path):
    b = telegram_bot.LawBot(TOKEN, {42}, "http://api", delivered=tmp_path / "d.txt", notify_sources={"cli"})
    sent: list[str] = []

    async def capture(markdown: str) -> None:
        sent.append(markdown.splitlines()[0])

    b._broadcast = capture  # type: ignore[method-assign]
    running = _job()
    await b._notify(running)
    await b._notify(running)  # second poll: nothing new
    await b._notify({**running, "status": "done", "result": {"answer": "ok"}})
    await b._notify({**running, "status": "done", "result": {"answer": "ok"}})
    assert [s.split()[1] for s in sent] == ["**Started**", "**Done**"]


def test_split_dir_uses_an_existing_folder(tmp_path):
    from law.telegram_bot import split_dir

    assert split_dir(f"{tmp_path} add a login page") == {"dir": str(tmp_path), "prompt": "add a login page"}
    assert split_dir("Build a todo app") == {"prompt": "Build a todo app"}


def test_queued_ahead_counts_older_unfinished_top_level_jobs():
    from law.telegram_bot import queued_ahead

    jobs = [  # newest first, as GET /jobs returns them
        {"id": "new", "parent": None, "status": "queued"},
        {"id": "a", "parent": None, "status": "running"},
        {"id": "a1", "parent": "a", "status": "running"},  # a stage: counted through its root
        {"id": "b", "parent": None, "status": "done"},
        {"id": "c", "parent": None, "status": "waiting"},
    ]
    assert queued_ahead(jobs, "new") == 2
    assert queued_ahead(jobs, "missing") == 0


def test_cancelled_is_not_reported_as_failed():
    from law.telegram_bot import notice_finished, outcome_line

    assert outcome_line({"status": "cancelled", "error": "cancelled by user"}) == "🚫 cancelled"
    assert outcome_line({"status": "failed", "error": "boom"}) == "❌ failed: boom"
    assert notice_finished({"id": "j1", "kind": "task", "status": "cancelled"}).startswith("🚫 **Cancelled**")


def test_split_dir_accepts_a_new_folder_under_an_existing_parent(tmp_path):
    from law.telegram_bot import split_dir

    assert split_dir(f"{tmp_path}/new-app build it") == {"dir": f"{tmp_path}/new-app", "prompt": "build it"}
    assert split_dir(f"{tmp_path}/no/such build it") == {"prompt": f"{tmp_path}/no/such build it"}


def test_job_status_texts_for_telegram():
    import time

    from law.telegram_bot import job_card, job_line

    now = time.time()
    j = {"id": "abc123", "title": "LAN Chat", "health": "thinking", "kind": "project_new",
         "stage": {"index": 3, "total": 8, "role": "writer", "kind": "task_work", "status": "running",
                   "started": now - 600},
         "last_action": {"t": now - 420, "message": "✏️ writing docs/proposal/proposal.md"},
         "last_10min": {"reads": 2, "commands": 0, "edits": 1, "web": 0}, "last_write": now - 420,
         "plan": {"done": 4, "total": 15, "next": "Architecture", "progress": ""}, "error": None,
         "project": "/Users/x/SourceCode/lan-chat", "actions": ["cancel"]}
    line = job_line(j)
    assert line.startswith("🔵 thinking `abc123` **LAN Chat** · step 3/8 writer task_work") and "7 min ago" in line
    card = job_card(j, {"active": True, "last": {"tok_s": 7.6, "context": 98276}})
    assert "To-do: 4/15 done · next: Architecture" in card and "generating now" in card and "context 98K" in card


async def test_status_updates_edit_one_message_and_never_flood():
    from types import SimpleNamespace

    from law.telegram_bot import LawBot

    sent: list[str] = []
    edits: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat, text, **kw):  # type: ignore[no-untyped-def]
            sent.append(text)
            return SimpleNamespace(message_id=100 + len(sent))

        async def edit_message_text(self, text, chat_id, message_id, **kw):  # type: ignore[no-untyped-def]
            if message_id == 7:
                raise RuntimeError("Bad Request: message to edit not found")
            if edits and edits[-1] == (message_id, text):
                raise RuntimeError("Bad Request: message is not modified")
            edits.append((message_id, text))

    bot = LawBot.__new__(LawBot)
    bot.bot = FakeBot()  # type: ignore[assignment]
    reply = {"chat_id": 1, "status_id": 0}  # a job submitted elsewhere: no status message yet
    await bot._edit(reply, "… reading a")
    await bot._edit(reply, "… running b")
    await bot._edit(reply, "… running b")  # identical text
    assert sent == ["… reading a"] and edits == [(101, "… running b")]
    gone = {"chat_id": 1, "status_id": 7}  # a message that can no longer be edited
    await bot._edit(gone, "… x")
    await bot._edit(gone, "… y")
    assert sent[-1] == "… x" and gone["status_id"] == 102 and edits[-1] == (102, "… y")


def test_project_templates_and_quick_tasks_are_told_apart():
    import re as _re

    from law import projects

    assert projects.TEMPLATES["project"].startswith("/project new\n") and "proposal: brief" in projects.TEMPLATES["project"]
    assert projects.TEMPLATES["code"].startswith("/code new\n") and "proposal" not in projects.TEMPLATES["code"]
    template_like = lambda args: args.lower() == "init" or bool(_re.match(r"(?i)new\s*\n", args))  # noqa: E731
    assert template_like("new\nname: X") and template_like("init")
    assert not template_like("newsletter signup form") and not template_like("new feature: dark mode")


async def test_a_telegram_job_that_stops_unfollowed_is_reported_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Seen 2026-10-29: the Pantry pipeline (started days before the bot's last restart) failed; nobody followed it,
    so Telegram heard nothing."""
    import time

    b = telegram_bot.LawBot(TOKEN, {42}, "http://api", delivered=tmp_path / "d.txt")
    sent: list[str] = []
    offered: list[str] = []

    async def fake_send(chat: int, markdown: str) -> None:
        sent.append(markdown)

    async def fake_offer(chat: int, job: dict) -> None:
        offered.append(job["id"])

    monkeypatch.setattr(b, "_send", fake_send)
    monkeypatch.setattr(b, "_offer_recovery", fake_offer)
    now = time.time()
    job = {"id": "pipe", "kind": "project_step", "source": "telegram", "parent": None, "status": "failed",
           "error": "stage 13 failed: database is locked", "created": now - 4 * 86400, "finished": now,
           "reply_to": {"chat_id": 42}}
    assert b._stopped_unreported(job, now - 60)
    await b._report_stopped(job)
    assert offered == ["pipe"] and "database is locked" in sent[0]
    assert not b._stopped_unreported(job, now - 60)  # once only
    followed = {**job, "id": "followed"}
    b.following.add("followed")
    assert not b._stopped_unreported(followed, now - 60)  # its follower reports it
    old = {**job, "id": "old", "finished": now - 3600}
    assert not b._stopped_unreported(old, now - 60)  # stopped before this process started: not news
    again = {**job, "finished": now + 600}  # retried, then stopped again with nobody following
    assert b._stopped_unreported(again, now - 60)


@pytest.mark.parametrize("text,chat,status,age,repeat", [
    ("the same long message sent twice", 7, "running", 36, True),
    ("the same long message sent twice", 7, "done", 36, False),  # answered: asking again is a new request
    ("the same long message sent twice", 7, "waiting", 36, True),
    ("the same long message sent twice", 7, "failed", 36, False),  # sending it again after a failure is a retry
    ("the same long message sent twice", 8, "running", 36, False),
    ("the same long message sent twice", 7, "done", 700, False),
    ("yes", 7, "running", 36, False),
])
def test_a_double_send_is_told_from_a_new_request(text: str, chat: int, status: str, age: int, repeat: bool):
    job = {"kind": "ask", "parent": None, "status": status, "created": 1000.0, "payload": {"prompt": text},
           "reply_to": {"chat_id": 7}}
    assert telegram_bot.is_repeat([job], chat, text, 1000.0 + age) is repeat


async def test_typing_is_shown_and_renewed_until_the_limit():
    from types import SimpleNamespace

    sent: list[tuple[int, str]] = []

    async def action(chat: int, kind: str) -> None:
        sent.append((chat, kind))

    bot = SimpleNamespace(bot=SimpleNamespace(send_chat_action=action))
    await telegram_bot.LawBot._typing(bot, 7, every_s=0.01, limit_s=0.035)  # type: ignore[arg-type]
    assert sent and all(s == (7, "typing") for s in sent) and len(sent) >= 2


def test_an_agents_documents_are_attached(tmp_path):
    import os
    import time as _t

    folder = tmp_path / "out"
    (folder / ".law").mkdir(parents=True)
    (folder / "memo.md").write_text("memo")
    (folder / ".law" / "PROGRESS.md").write_text("notes")
    (folder / "old.pdf").write_text("old")
    os.utime(folder / "old.pdf", (_t.time() - 3600, _t.time() - 3600))
    job = {"kind": "task", "started": _t.time() - 60, "payload": {"dir": str(folder)}}
    assert [f.name for f in telegram_bot.deliverables(job)] == ["memo.md"]
    assert telegram_bot.deliverables({**job, "kind": "ask"}) == []
    assert telegram_bot.deliverables({**job, "payload": {"dir": str(folder), "project": "p"}}) == []


def test_files_the_agent_sent_itself_are_not_attached_again(tmp_path, monkeypatch):
    import time as _t

    from law import deliver

    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "a.pdf").write_text("a")
    (folder / "b.md").write_text("b")
    log = tmp_path / "data" / deliver.SENT_LOG
    deliver.record_sent(log, [folder / "a.pdf"])
    real = deliver.already_sent
    monkeypatch.setattr(deliver, "already_sent", lambda _p: real(log))
    job = {"kind": "task", "started": _t.time() - 60, "payload": {"dir": str(folder)}}
    assert [f.name for f in telegram_bot.deliverables(job)] == ["b.md"]


async def test_a_rate_limited_edit_does_not_post_a_new_message():
    from types import SimpleNamespace

    posted: list[str] = []

    async def edit(*_a, **_k):
        raise RuntimeError("Telegram server says - Too Many Requests: retry after 5")

    async def send(chat, text, **_k):
        posted.append(text)
        return SimpleNamespace(message_id=2)

    bot = SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit, send_message=send))
    reply = {"chat_id": 7, "status_id": 1}
    await telegram_bot.LawBot._edit(bot, reply, "… writing", rich=False)  # type: ignore[arg-type]
    assert posted == [] and reply["status_id"] == 1


async def test_long_messages_such_as_help_are_split_under_telegrams_limit():
    from types import SimpleNamespace

    sent: list[str] = []

    async def send(chat, text, **_k):
        sent.append(text)

    bot = SimpleNamespace(bot=SimpleNamespace(send_message=send))
    await telegram_bot.LawBot._send(bot, 7, telegram_bot.HELP_TEXT)  # type: ignore[arg-type]
    assert len(sent) >= 2 and all(len(x) <= 4096 for x in sent)
    assert "Projects cheat sheet" in "".join(sent) and "/amend" in "".join(sent)
