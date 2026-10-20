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

    async def fake_deliver(job_id, reply):  # type: ignore[no-untyped-def]
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
    assert split_dir(f"{tmp_path}/missing fix it") == {"prompt": f"{tmp_path}/missing fix it"}


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
