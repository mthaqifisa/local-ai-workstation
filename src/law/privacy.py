"""Keep the owner's personal data out of files that land in the public repo (CLAUDE.md, "Privacy").

Personal values are never written into code: they are read at runtime from `~/LAW/state/.env` and
`~/LAW/state/handover.env`, and `scrub` replaces them (and the home folder) before a committed file is written.
"""

from __future__ import annotations

import re
from pathlib import Path

from law import secrets
from law.paths import LawPaths, display

PLACEHOLDER_EMAIL = "you@example.com"
PLACEHOLDER_ID = "[telegram-id]"


def personal_values(paths: LawPaths) -> list[str]:
    """The owner's email addresses and Telegram IDs known on this machine (longest first)."""
    env = secrets.read(paths.env_file)
    handover = secrets.read(paths.state / "handover.env")
    values = {env.get("LAW_REPORT_EMAIL", ""), handover.get("report_email", "")}
    values.update(re.split(r"[\s,]+", env.get("LAW_TELEGRAM_USER_IDS", "")))
    return sorted((v for v in values if len(v) >= 5), key=len, reverse=True)


def scrub(text: str, paths: LawPaths | None = None) -> str:
    """`text` with the home folder shown as `~` and the owner's email and Telegram IDs replaced."""
    p = paths or LawPaths.from_env()
    out = display(text, p.user_home)
    for value in personal_values(p):
        if "@" in value:
            out = re.sub(re.escape(value), PLACEHOLDER_EMAIL, out, flags=re.IGNORECASE)
        else:
            out = re.sub(rf"(?<![0-9]){re.escape(value)}(?![0-9])", PLACEHOLDER_ID, out)
    return out


def gitleaks_config(start: Path) -> Path | None:
    """The repo's `.gitleaks.toml` (default secret rules plus privacy rules), searching up from `start`."""
    for folder in (start, *start.parents):
        cfg = folder / ".gitleaks.toml"
        if cfg.is_file():
            return cfg
    return None
