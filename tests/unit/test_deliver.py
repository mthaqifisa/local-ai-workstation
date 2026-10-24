"""`law send`: files reach only the allow-listed Telegram chats and the owner's email; failures are clear."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from law import deliver, mailer

TOKEN = "123:secret-token"


def env(tmp_path: Path, **values: str) -> Path:
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def pdf(tmp_path: Path, name: str = "report.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.7 test")
    return path


def test_each_file_goes_as_a_document_to_every_allowed_chat(tmp_path):
    sent: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, request.content))
        return httpx.Response(200, json={"ok": True})

    files = [pdf(tmp_path), pdf(tmp_path, "chart.png")]
    chats = deliver.to_telegram(env(tmp_path, LAW_TELEGRAM_TOKEN=TOKEN, LAW_TELEGRAM_USER_IDS="11,22"), files,
                                "LAW v6 report", transport=httpx.MockTransport(handler))
    assert chats == 2 and len(sent) == 4
    assert all(path == f"/bot{TOKEN}/sendDocument" for path, _ in sent)
    assert b"LAW v6 report" in sent[0][1] and b"LAW v6 report" not in sent[1][1]  # caption on the first file only
    assert b"report.pdf" in sent[0][1] and b'name="chat_id"\r\n\r\n22' in sent[2][1]


def test_a_message_alone_is_sent_as_text(tmp_path):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    deliver.to_telegram(env(tmp_path, LAW_TELEGRAM_TOKEN=TOKEN, LAW_TELEGRAM_USER_IDS="11"), [], "done",
                        transport=httpx.MockTransport(handler))
    assert paths == [f"/bot{TOKEN}/sendMessage"]


def test_a_refused_upload_is_an_error_that_never_shows_the_token(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(deliver.DeliveryError) as exc:
        deliver.to_telegram(env(tmp_path, LAW_TELEGRAM_TOKEN=TOKEN, LAW_TELEGRAM_USER_IDS="11"), [pdf(tmp_path)],
                            transport=httpx.MockTransport(handler))
    assert "chat not found" in str(exc.value) and "secret-token" not in str(exc.value)


@pytest.mark.parametrize("values", [{}, {"LAW_TELEGRAM_TOKEN": TOKEN}, {"LAW_TELEGRAM_USER_IDS": "11"}])
def test_telegram_needs_the_token_and_the_allow_list(tmp_path, values):
    with pytest.raises(deliver.DeliveryError, match="not set up"):
        deliver.to_telegram(env(tmp_path, **values), [pdf(tmp_path)])


def test_missing_files_fail_before_anything_is_sent(tmp_path):
    with pytest.raises(deliver.DeliveryError, match="file not found"):
        deliver.to_telegram(env(tmp_path, LAW_TELEGRAM_TOKEN=TOKEN, LAW_TELEGRAM_USER_IDS="11"),
                            [tmp_path / "nope.pdf"], transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def test_email_goes_to_the_report_address_unless_another_is_given(tmp_path, monkeypatch):
    calls: list[tuple[str, str, list[Path]]] = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, files, work: calls.append((to, subject, files)))
    env_file = env(tmp_path, LAW_REPORT_EMAIL="owner@example.com")
    f = pdf(tmp_path)
    assert deliver.by_email(env_file, tmp_path, [f], "Report", "attached") == "owner@example.com"
    assert deliver.by_email(env_file, tmp_path, [f], "Report", "", to="other@example.org") == "other@example.org"
    assert calls == [("owner@example.com", "Report", [f]), ("other@example.org", "Report", [f])]


def test_email_errors_are_delivery_errors(tmp_path, monkeypatch):
    def refuse(*_a: object) -> None:
        raise mailer.MailError("Mail did not respond")

    monkeypatch.setattr(mailer, "send", refuse)
    with pytest.raises(deliver.DeliveryError, match="no report email"):
        deliver.by_email(env(tmp_path), tmp_path, [pdf(tmp_path)], "s", "m")
    with pytest.raises(deliver.DeliveryError, match="not an email address"):
        deliver.by_email(env(tmp_path), tmp_path, [pdf(tmp_path)], "s", "m", to="nobody")
    with pytest.raises(deliver.DeliveryError, match="did not respond"):
        deliver.by_email(env(tmp_path, LAW_REPORT_EMAIL="o@example.com"), tmp_path, [pdf(tmp_path)], "s", "m")
