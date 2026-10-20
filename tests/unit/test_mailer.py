"""Report email: address from ~/LAW/state/.env, sent via Mail.app (osascript faked)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from law import mailer, secrets


def test_recipient_comes_from_the_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    with pytest.raises(mailer.MailError, match="no report email"):
        mailer.recipient(env)
    secrets.write(env, {"LAW_REPORT_EMAIL": "me@example.com"})
    assert mailer.recipient(env) == "me@example.com"


def test_send_passes_body_and_attachments_to_mail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "proposal.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        seen["cmd"] = cmd
        seen["body"] = Path(cmd[4]).read_text()
        return SimpleNamespace(returncode=0, stdout="sent\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    mailer.send("me@example.com", "Proposal", "Summary", [pdf], tmp_path / "work")
    assert seen["cmd"][0] == "osascript" and seen["cmd"][2:4] == ["me@example.com", "Proposal"]
    assert seen["cmd"][-1] == str(pdf) and seen["body"] == "Summary"


def test_send_reports_mail_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="denied"))
    with pytest.raises(mailer.MailError, match="denied"):
        mailer.send("me@example.com", "s", "b", [], tmp_path)
    with pytest.raises(mailer.MailError, match="attachment missing"):
        mailer.send("me@example.com", "s", "b", [tmp_path / "nope.pdf"], tmp_path)
