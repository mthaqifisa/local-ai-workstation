"""Email reports (e.g. /proposal PDFs) to the owner through the Mac's Mail app.

The address is LAW_REPORT_EMAIL in ~/LAW/state/.env (asked by the installer, editable in the Console). Mail uses
the account already signed in on this Mac, so LAW stores no mail password.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from law import secrets

SCRIPT = Path(__file__).parent / "tools" / "send_mail.applescript"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KEY = "LAW_REPORT_EMAIL"


class MailError(RuntimeError):
    pass


def valid(address: str) -> bool:
    return bool(EMAIL_RE.match(address.strip()))


def recipient(env_file: Path) -> str:
    address = secrets.read(env_file).get(KEY, "").strip()
    if not valid(address):
        raise MailError("no report email set: add it in the LAW Console (Settings) or re-run ./install.sh")
    return address


def send(to: str, subject: str, body: str, attachments: list[Path], work: Path, timeout_s: float = 120) -> None:
    """Send through Mail.app. The first send asks macOS for permission to control Mail (Automation)."""
    for path in attachments:
        if not path.is_file():
            raise MailError(f"attachment missing: {path}")
    work.mkdir(parents=True, exist_ok=True)
    body_file = work / "mail-body.txt"
    body_file.write_text(body)
    cmd = ["osascript", str(SCRIPT), to, subject, str(body_file), *(str(p) for p in attachments)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise MailError("Mail did not respond: allow LAW to control Mail in System Settings > Privacy & "
                        "Security > Automation, then retry") from exc
    if out.returncode != 0 or "sent" not in out.stdout:
        raise MailError(f"Mail could not send: {(out.stderr or out.stdout).strip()[-400:]}")
