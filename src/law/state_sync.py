"""Portable Open WebUI state: `law save` exports it into the repo, `law restore-state` imports it on any Mac.

Covered: model presets, prompts, tools, functions and knowledge-base definitions (name and description; the
documents themselves belong in `knowledge/`). Not covered, on purpose: global config (the installer owns it),
tool/function valves and anything per user, because those are where API keys and personal data live.

Everything is written to a staging folder first and scanned for secrets; only a clean export replaces
`settings/openwebui/`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

# Keys dropped from every exported record: per-user, per-install or possibly secret.
DROP = {"user_id", "user", "created_at", "updated_at", "access_grants", "access_control", "valves", "is_active",
        "version_id", "write_access", "files"}
SECRET = re.compile(r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|\d{8,10}:[A-Za-z0-9_-]{35}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16})")
KINDS = ("models", "prompts", "tools", "functions", "knowledge")


class StateError(RuntimeError):
    pass


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in sorted(value.items()) if k not in DROP}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


class OpenWebUI:
    def __init__(self, base: str, timeout: float = 30, transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(base_url=base.rstrip("/"), timeout=timeout, transport=transport)
        token = self.http.post("/api/v1/auths/signin", json={"email": "", "password": ""}).json().get("token")
        if not token:
            raise StateError("Open WebUI sign-in failed (is WEBUI_AUTH=False?)")
        self.http.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str) -> Any:
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any) -> Any:
        r = self.http.post(path, json=body)
        if r.status_code >= 400:
            raise StateError(f"POST {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    def export(self) -> dict[str, list[dict[str, Any]]]:
        knowledge = self.get("/api/v1/knowledge/")
        items = knowledge.get("items", []) if isinstance(knowledge, dict) else knowledge
        return {
            "models": _clean(self.get("/api/v1/models/export")),
            "prompts": [{k: v for k, v in p.items() if k != "id"} for p in _clean(self.get("/api/v1/prompts/"))],
            "tools": _clean(self.get("/api/v1/tools/export")),
            "functions": _clean(self.get("/api/v1/functions/export")),
            "knowledge": [{"name": k["name"], "description": k.get("description", "")} for k in items],
        }

    def restore(self, state: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        """Add what is missing; never overwrite or delete what the user created on this Mac."""
        added = dict.fromkeys(KINDS, 0)
        if state.get("models"):
            self.post("/api/v1/models/import", {"models": state["models"]})
            added["models"] = len(state["models"])
        have = {p["command"] for p in self.get("/api/v1/prompts/")}
        for p in state.get("prompts", []):
            if p["command"] not in have:
                self.post("/api/v1/prompts/create", p)
                added["prompts"] += 1
        for kind in ("tools", "functions"):
            have = {t["id"] for t in self.get(f"/api/v1/{kind}/")}
            for t in state.get(kind, []):
                if t["id"] not in have:
                    self.post(f"/api/v1/{kind}/create", {k: t[k] for k in ("id", "name", "content", "meta") if k in t})
                    added[kind] += 1
        knowledge = self.get("/api/v1/knowledge/")
        have = {k["name"] for k in (knowledge.get("items", []) if isinstance(knowledge, dict) else knowledge)}
        for k in state.get("knowledge", []):
            if k["name"] not in have:
                self.post("/api/v1/knowledge/create", k)
                added["knowledge"] += 1
        return added


def secret_hits(folder: Path, gitleaks: str | None = None) -> list[str]:
    """Secret-looking strings in the export (own patterns plus gitleaks when installed)."""
    hits = [f"{f.name}: {m.group(0)[:12]}…" for f in sorted(folder.glob("*.json"))
            for m in SECRET.finditer(f.read_text())]
    tool = gitleaks or shutil.which("gitleaks")
    if tool:
        res = subprocess.run([tool, "dir", "--no-banner", "--exit-code", "1", str(folder)],
                             capture_output=True, text=True, check=False)
        if res.returncode == 1:
            hits.append("gitleaks: " + (res.stdout + res.stderr).strip()[-300:])
    return hits


def save(api: OpenWebUI, target: Path, staging: Path) -> list[str]:
    """Export into `target` (settings/openwebui). Returns the files written; raises on any secret."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for kind, records in api.export().items():
        (staging / f"{kind}.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
    hits = secret_hits(staging)
    if hits:
        raise StateError("not saved, possible secrets in the export:\n" + "\n".join(hits))
    target.mkdir(parents=True, exist_ok=True)
    for f in sorted(staging.glob("*.json")):
        shutil.copyfile(f, target / f.name)
    shutil.rmtree(staging)
    return [str(target / f"{k}.json") for k in KINDS]


def load(target: Path) -> dict[str, list[dict[str, Any]]]:
    return {k: json.loads((target / f"{k}.json").read_text()) for k in KINDS if (target / f"{k}.json").exists()}
