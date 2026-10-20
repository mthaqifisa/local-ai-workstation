"""Interactive Telegram setup with Telegram's API faked."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from law import telegram_setup as ts

GOOD = "123456789:" + "A" * 35
BOT = {"username": "TonyBot", "first_name": "Tony"}
SENDERS = [(512345678, "Alex (@alex)"), (698765432, "Guest")]


def run(answers: list[str], secret: list[str]) -> tuple[dict[str, str], list[str]]:
    a: Iterator[str] = iter(answers)
    s: Iterator[str] = iter(secret)
    said: list[str] = []
    out = ts.configure(ask=lambda _p: next(a), ask_secret=lambda _p: next(s), say=said.append)
    return out, said


@pytest.fixture()
def telegram(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"senders": list(SENDERS)}
    monkeypatch.setattr(ts, "get_me", lambda token: BOT if token == GOOD else None)
    monkeypatch.setattr(ts, "recent_senders", lambda token: state["senders"])
    return state


def test_skip_returns_nothing(telegram):
    out, said = run([], [""])
    assert out == {} and any("Skipped" in s for s in said)


def test_valid_token_and_pick_detected_sender(telegram):
    out, said = run(["", "1"], [GOOD])
    assert out == {"LAW_TELEGRAM_TOKEN": GOOD, "LAW_TELEGRAM_USER_IDS": "512345678"}
    assert any("Connected to @TonyBot" in s for s in said)


def test_pick_several_senders(telegram):
    out, _ = run(["", "1,2"], [GOOD])
    assert out["LAW_TELEGRAM_USER_IDS"] == "512345678,698765432"


def test_bad_format_then_rejected_then_good(telegram):
    out, said = run(["", ""], ["not-a-token", "999999:" + "B" * 35, GOOD])
    assert out["LAW_TELEGRAM_TOKEN"] == GOOD
    assert any("doesn't look like" in s for s in said) and any("rejected" in s for s in said)


def test_manual_ids_when_nobody_messaged(telegram):
    telegram["senders"] = []
    out, _ = run(["", "m", "11111111, 22222222"], [GOOD])
    assert out["LAW_TELEGRAM_USER_IDS"] == "11111111,22222222"


def test_no_ids_means_no_bot(telegram):
    telegram["senders"] = []
    out, said = run(["", "m", ""], [GOOD])
    assert out == {} and any("ignore everyone" in s for s in said)


def test_recent_senders_parses_updates(monkeypatch: pytest.MonkeyPatch):
    class R:
        def json(self) -> dict[str, object]:
            return {"ok": True, "result": [
                {"message": {"from": {"id": 1, "first_name": "Old", "is_bot": False}}},
                {"message": {"from": {"id": 2, "first_name": "Alex", "username": "alex", "is_bot": False}}},
                {"message": {"from": {"id": 99, "first_name": "SomeBot", "is_bot": True}}},
                {"edited_message": {"from": {"id": 2, "first_name": "Alex", "is_bot": False}}},
            ]}

    monkeypatch.setattr(ts.httpx, "get", lambda *a, **k: R())
    assert ts.recent_senders(GOOD) == [(2, "Alex (@alex)"), (1, "Old")]


def test_ask_email_retries_until_valid_and_can_be_skipped():
    from law.telegram_setup import ask_email

    answers = iter(["nope", "me@example.com"])
    assert ask_email(ask=lambda _: next(answers), say=lambda _: None) == {"LAW_REPORT_EMAIL": "me@example.com"}
    assert ask_email(ask=lambda _: "", say=lambda _: None) == {}
