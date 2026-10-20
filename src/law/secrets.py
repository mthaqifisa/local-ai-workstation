"""Secrets live only in ~/LAW/state/.env (0600) and are never written into the repo."""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path

GENERATED = ("LAW_SERVICE_KEY", "WEBUI_SECRET_KEY", "SEARXNG_SECRET")
# LAW_REPORT_EMAIL is personal rather than secret, but it must stay out of the public repo too.
USER_PROVIDED = ("LAW_TELEGRAM_TOKEN", "LAW_TELEGRAM_USER_IDS", "HF_TOKEN", "LAW_REPORT_EMAIL")


def read(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# LAW v6 secrets — machine-local, never commit.\n" + "".join(
        f"{k}={v}\n" for k, v in sorted(values.items()))
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    tmp.replace(path)
    path.chmod(0o600)


def ensure(path: Path, *, imported: dict[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Fill generated keys, take user values from `imported` or the environment.

    Returns (values, changed_keys). Existing values are never replaced unless explicitly imported.
    """
    values = read(path)
    changed: list[str] = []
    for key in GENERATED:
        if not values.get(key):
            values[key] = _secrets.token_urlsafe(32)
            changed.append(key)
    for key in USER_PROVIDED:
        new = (imported or {}).get(key) or os.environ.get(key)
        if new and values.get(key) != new:
            values[key] = new
            changed.append(key)
    if changed or not path.exists() or (path.stat().st_mode & 0o777) != 0o600:
        write(path, values)
    return values, changed


def allowed_telegram_ids(values: dict[str, str]) -> set[int]:
    raw = values.get("LAW_TELEGRAM_USER_IDS", "")
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}
