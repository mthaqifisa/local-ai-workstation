"""Console write actions: edit a persona in settings/roles.yaml, update Telegram settings.

Both re-apply configuration through the installer afterwards, so the console never diverges from
what `law install` would produce.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from law import registry, roles, secrets, telegram_setup
from law.paths import LawPaths

KNOWN_TOOLS = {"read", "files", "shell", "search", "browser", "vision", "docs", "mac", "python"}


class RoleUpdate(BaseModel):
    summary: str = Field(min_length=3, max_length=200)
    prompt: str = Field(min_length=20, max_length=20000)
    temperature: float = Field(ge=0, le=1)
    reasoning: Literal["off", "low", "medium", "xhigh"]
    tools: list[str]


class EmailUpdate(BaseModel):
    address: str


class TelegramUpdate(BaseModel):
    token: str | None = None
    user_ids: str


def list_roles(paths: LawPaths) -> dict[str, Any]:
    loaded = roles.load(paths.settings / "roles.yaml")
    reg = registry.load(paths.registry_file)
    return {name: {**r.model_dump(), "model": reg.model_for_role(r.model_role).id}
            for name, r in loaded.roles.items()}


def update_role(paths: LawPaths, name: str, upd: RoleUpdate) -> None:
    unknown = set(upd.tools) - KNOWN_TOOLS
    if unknown:
        raise ValueError(f"unknown tools: {', '.join(sorted(unknown))}")
    path = paths.settings / "roles.yaml"
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    doc = yaml.load(path.read_text())
    if name not in doc["roles"]:
        raise KeyError(name)
    role = doc["roles"][name]
    role["summary"] = upd.summary
    role["prompt"] = LiteralScalarString(upd.prompt.rstrip() + "\n")
    role["temperature"] = upd.temperature
    role["reasoning"] = upd.reasoning
    role["tools"] = upd.tools
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w") as fh:
        yaml.dump(doc, fh)
    roles.load(tmp)  # validate before replacing the real file
    tmp.replace(path)


def telegram_status(paths: LawPaths) -> dict[str, Any]:
    values = secrets.read(paths.env_file)
    token = values.get("LAW_TELEGRAM_TOKEN", "")
    bot = telegram_setup.get_me(token) if token else None
    return {"configured": bool(token), "bot": (bot or {}).get("username"),
            "user_ids": [str(i) for i in sorted(secrets.allowed_telegram_ids(values))]}


def update_telegram(paths: LawPaths, upd: TelegramUpdate) -> str:
    ids = [x.strip() for x in upd.user_ids.replace(";", ",").split(",") if x.strip()]
    if not ids or not all(x.isdigit() for x in ids):
        raise ValueError("user IDs must be numbers, e.g. 512345678")
    current = secrets.read(paths.env_file).get("LAW_TELEGRAM_TOKEN", "")
    token = (upd.token or "").strip() or current
    if not token:
        raise ValueError("no bot token set")
    bot = telegram_setup.get_me(token)
    if bot is None:
        raise ValueError("Telegram rejected the token")
    secrets.ensure(paths.env_file, imported={"LAW_TELEGRAM_TOKEN": token, "LAW_TELEGRAM_USER_IDS": ",".join(ids)})
    return str(bot.get("username"))


def email_status(paths: LawPaths) -> dict[str, Any]:
    return {"address": secrets.read(paths.env_file).get("LAW_REPORT_EMAIL", "")}


def update_email(paths: LawPaths, upd: EmailUpdate) -> str:
    from law.mailer import valid

    address = upd.address.strip()
    if not valid(address):
        raise ValueError("not a valid email address")
    secrets.ensure(paths.env_file, imported={"LAW_REPORT_EMAIL": address})
    return address


class StorageUpdate(BaseModel):
    jobs_days: int = Field(ge=1, le=3650)
    agent_sessions_days: int = Field(ge=1, le=3650)
    logs_days: int = Field(ge=1, le=3650)
    trash_days: int = Field(ge=1, le=3650)
    llm_ssd_cache_gb: int = Field(ge=1, le=500)


def storage_settings(paths: LawPaths) -> dict[str, Any]:
    reg = registry.load(paths.registry_file)
    return {**reg.retention.model_dump(), "llm_ssd_cache_gb": reg.machine.llm_ssd_cache_gb}


def update_storage(paths: LawPaths, upd: StorageUpdate) -> bool:
    """Write retention and the prompt-cache cap into settings/registry.yaml (comments kept). Returns True when the
    cache cap changed, which needs the omlx-settings install step."""
    path = paths.registry_file
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    doc = yaml.load(path.read_text())
    cap_changed = int(doc["machine"].get("llm_ssd_cache_gb", 20)) != upd.llm_ssd_cache_gb
    doc["machine"]["llm_ssd_cache_gb"] = upd.llm_ssd_cache_gb
    keep = doc.setdefault("retention", {})
    for key in ("jobs_days", "agent_sessions_days", "logs_days", "trash_days"):
        keep[key] = getattr(upd, key)
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w") as fh:
        yaml.dump(doc, fh)
    registry.load(tmp)  # validate before replacing the real file
    tmp.replace(path)
    return cap_changed


def apply_async(paths: LawPaths, steps: str) -> None:
    """Re-run the relevant install steps in the background (they may restart this API)."""
    law = paths.runtime / "law-venv" / "bin" / "law"
    log = (paths.logs / "console-apply.log").open("a")
    subprocess.Popen([str(law), "install", "--only", steps], stdin=subprocess.DEVNULL, stdout=log,
                     stderr=subprocess.STDOUT, start_new_session=True, cwd=str(Path(paths.repo)))
