"""OpenCode (terminal coding agent) wired to the law gateway.

law keeps ONE provider block, "law", in ~/.config/opencode/opencode.json:

  "provider": {
    "law": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local AI Workstation (law)",
      "options": {"baseURL": "http://127.0.0.1:4000/v1", "apiKey": "local"},
      "models": {"coder": {"name": "coder · qwen3-coder-next", "limit": {"context": 32768, "output": 8192}}, …}
    }
  }

Every persona whose model can chat becomes a model (law/coder, law/general, …) with the
context limit from the registry, so OpenCode compacts the conversation before the model's
proven limit. "model": "law/coder" is set only if you haven't chosen a model yourself.
Everything else in the file (your other providers, settings) is left exactly as it is; the
first time law changes the file, a copy is saved as opencode.json.law-backup.

Requests from OpenCode go through the gateway → queue, so they take turns with everything
else. Requests with tools are answered as one whole reply (runtime.gateway.tools_whole_reply)
and tool calls the model wrote as text are recovered.

Only a plain-JSON opencode.json is edited. If you keep your settings in opencode.jsonc
(JSON with comments), law doesn't touch it — `law setup-opencode --print` shows the block
to paste.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .registry import Registry
from .util import atomic_write, which

SCHEMA = "https://opencode.ai/config.json"
PROVIDER_ID = "law"
CHAT_KINDS = ("lm", "vlm")


def config_dir(home: Path) -> Path:
    return home / ".config" / "opencode"


def config_file(home: Path) -> Path:
    return config_dir(home) / "opencode.json"


def provider_block(reg: Registry) -> dict:
    agent = (reg.raw.get("runtime") or {}).get("agent") or {}
    out_tokens = int(agent.get("max_tokens", 8192))
    models = {}
    for name, p in reg.personas.items():
        m = reg.models.get(p["model"])
        if m is None or m.kind not in CHAT_KINDS:
            continue
        models[name] = {"name": f"{name} · {m.id}",
                        "limit": {"context": int(m.context or 32768), "output": out_tokens}}
    return {"npm": "@ai-sdk/openai-compatible",
            "name": "Local AI Workstation (law)",
            "options": {"baseURL": f"http://127.0.0.1:{reg.ports['gateway']}/v1", "apiKey": "local"},
            "models": models}


def in_use(home: Path) -> bool:
    """Manage the config only if OpenCode is installed or already configured."""
    return bool(which("opencode")) or config_dir(home).is_dir()


def desired(current: dict, reg: Registry) -> dict:
    want = copy.deepcopy(current)
    want.setdefault("$schema", SCHEMA)
    providers = want.get("provider")
    if not isinstance(providers, dict):
        providers = want["provider"] = {}
    providers[PROVIDER_ID] = provider_block(reg)
    default = ((reg.raw.get("runtime") or {}).get("opencode") or {}).get("default_model", "coder")
    if not want.get("model") and default in providers[PROVIDER_ID]["models"]:
        want["model"] = f"{PROVIDER_ID}/{default}"
    return want


class JsoncConfig(RuntimeError):
    pass


def read_current(home: Path) -> dict:
    f = config_file(home)
    if not f.exists():
        if (config_dir(home) / "opencode.jsonc").exists():
            raise JsoncConfig("your OpenCode settings are in opencode.jsonc (with comments) — law won't "
                              "rewrite it. Paste the block from `law setup-opencode --print` into it.")
        return {}
    try:
        data = json.loads(f.read_text())
    except ValueError as e:
        raise JsoncConfig(f"{f} is not plain JSON ({e}) — law won't rewrite it. Paste the block from "
                          "`law setup-opencode --print` into it.") from e
    if not isinstance(data, dict):
        raise JsoncConfig(f"{f} does not contain a JSON object")
    return data


def write(home: Path, data: dict) -> Path:
    f = config_file(home)
    backup = f.with_name(f.name + ".law-backup")
    if f.exists() and not backup.exists():
        backup.write_bytes(f.read_bytes())
    atomic_write(f, json.dumps(data, indent=2) + "\n")
    return f


def needs_update(home: Path, reg: Registry) -> tuple[bool, dict]:
    cur = read_current(home)
    want = desired(cur, reg)
    return want != cur, want
