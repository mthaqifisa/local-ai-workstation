"""Load + validate the registry (built-in registry.yaml merged with the user overlay)."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .paths import Paths

KINDS = {"lm", "vlm", "embed", "encoder", "tts", "stt", "image", "video", "music"}
TIERS = {"core", "heavy", "optional", "user"}


class RegistryError(ValueError):
    pass


@dataclass
class Model:
    id: str
    repo: str
    kind: str
    weights_gb: float
    tier: str = "optional"
    context: int | None = None
    license: str = "unknown"
    tool_parser: str | None = None
    reasoning_parser: str | None = None
    verified: bool = False
    download: str = "manual"
    label: str = ""
    source: str = "builtin"


@dataclass
class Registry:
    raw: dict[str, Any]
    models: dict[str, Model] = field(default_factory=dict)

    @property
    def ports(self) -> dict[str, int]:
        return dict(self.raw["ports"])

    @property
    def max_weights_gb(self) -> float:
        return float(self.raw["machine"]["max_weights_gb"])

    @property
    def personas(self) -> dict[str, dict]:
        return dict(self.raw.get("personas", {}))

    @property
    def brew_formulae(self) -> list[str]:
        return list(self.raw.get("brew", {}).get("formulae", []))

    @property
    def python(self) -> dict:
        return dict(self.raw.get("python", {}))

    @property
    def docker(self) -> dict:
        return dict(self.raw.get("docker", {}))

    @property
    def bind_host(self) -> str:
        # Shipped default is 127.0.0.1 (this Mac only — safe, no auth). To expose the gateway/dashboard
        # on your LAN, set LAW_BIND_HOST=0.0.0.0 in ~/.law/.env (or network.bind_host in the registry) —
        # but ONLY on a network you trust: there is no authentication. The env var wins so a public clone
        # stays safe by default while your own machine opts in without editing the shipped registry.
        import os
        env = (os.environ.get("LAW_BIND_HOST") or "").strip()
        if not env:                                   # also honor ~/.law/.env (no plist/registry edit needed)
            try:
                from .paths import get_paths
                from .util import read_env
                env = (read_env(get_paths().env_file).get("LAW_BIND_HOST") or "").strip()
            except Exception:  # noqa: BLE001
                env = ""
        return env or str(self.raw.get("network", {}).get("bind_host", "127.0.0.1"))

    def model(self, mid: str) -> Model:
        if mid not in self.models:
            raise RegistryError(f"unknown model id '{mid}'")
        return self.models[mid]

    def fits(self, mid: str) -> tuple[bool, str]:
        m = self.model(mid)
        if m.weights_gb > self.max_weights_gb:
            return False, (f"{m.id} needs ~{m.weights_gb:g} GB weights; limit is "
                           f"{self.max_weights_gb:g} GB on this machine")
        return True, "ok"


def _builtin_text() -> str:
    return resources.files("law").joinpath("registry.yaml").read_text()


def _validate(raw: dict[str, Any]) -> dict[str, Model]:
    for key in ("schema", "machine", "ports", "models", "personas"):
        if key not in raw:
            raise RegistryError(f"registry missing '{key}'")
    ports = raw["ports"]
    seen: dict[int, str] = {}
    for name, port in ports.items():
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise RegistryError(f"port {name}={port!r} invalid")
        if port in seen:
            raise RegistryError(f"port {port} used by both {seen[port]} and {name}")
        seen[port] = name

    models: dict[str, Model] = {}
    for entry in raw["models"]:
        missing = {"id", "repo", "kind", "weights_gb"} - set(entry)
        if missing:
            raise RegistryError(f"model {entry.get('id', '?')} missing {sorted(missing)}")
        if entry["kind"] not in KINDS:
            raise RegistryError(f"model {entry['id']}: kind '{entry['kind']}' not in {sorted(KINDS)}")
        if entry.get("tier", "optional") not in TIERS:
            raise RegistryError(f"model {entry['id']}: tier '{entry.get('tier')}' invalid")
        if entry.get("download", "manual") not in ("auto", "manual"):
            raise RegistryError(f"model {entry['id']}: download must be auto|manual")
        if "/" not in entry["repo"]:
            raise RegistryError(f"model {entry['id']}: repo '{entry['repo']}' must be org/name")
        allowed = set(Model.__dataclass_fields__)
        models[entry["id"]] = Model(**{k: v for k, v in entry.items() if k in allowed})

    for pname, p in raw["personas"].items():
        if p.get("model") not in models:
            raise RegistryError(f"persona '{pname}' points to unknown model '{p.get('model')}'")
    for cname, c in (raw.get("docker", {}).get("containers") or {}).items():
        if c.get("port") not in ports:
            raise RegistryError(f"container '{cname}' uses unknown port name '{c.get('port')}'")
    d = raw.get("defaults", {})
    if d.get("model") and d["model"] not in models:
        raise RegistryError(f"defaults.model '{d['model']}' unknown")
    if d.get("persona") and d["persona"] not in raw["personas"]:
        raise RegistryError(f"defaults.persona '{d['persona']}' unknown")
    return models


def load(paths: Paths | None = None, builtin_text: str | None = None) -> Registry:
    raw = yaml.safe_load(builtin_text or _builtin_text())
    if not isinstance(raw, dict):
        raise RegistryError("registry.yaml is not a mapping")
    raw = copy.deepcopy(raw)

    if paths is not None and paths.user_models.exists():
        user = yaml.safe_load(paths.user_models.read_text()) or {}
        by_id = {m["id"]: i for i, m in enumerate(raw["models"])}
        for m in user.get("models", []) or []:
            if m.get("id") in by_id:                    # override fields of a built-in model
                i = by_id[m["id"]]
                raw["models"][i] = {**raw["models"][i], **m, "source": "override"}
            else:                                       # a new model of your own
                raw["models"].append({"tier": "user", **m, "source": "user"})
        for name, p in (user.get("personas") or {}).items():
            raw["personas"][name] = {**raw["personas"].get(name, {}), **p}

    reg = Registry(raw=raw)
    reg.models = _validate(raw)
    return reg


def builtin_ids() -> set[str]:
    return {m["id"] for m in yaml.safe_load(_builtin_text())["models"]}
