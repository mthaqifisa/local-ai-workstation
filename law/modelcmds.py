"""`law add-model` / `law remove-model` — manage YOUR models without editing YAML.

add-model <hf-repo>
  - asks Hugging Face whether the repo exists (and whether it's gated)
  - weights size = sum of the repo's weight files → used by the RAM guard and disk check
  - guesses kind (chat / vision / embeddings / tts) and tool/reasoning parsers from the
    repo; every guess can be overridden with a flag
  - refuses repos over the RAM limit (46 GB by default) unless --force
  - writes the entry to ~/.law/config/models.user.yaml (your file, kept across updates)
  - optional --pull downloads it straight away; --persona points a persona at it

remove-model <id>
  - removes YOUR model (or your override of a built-in model, restoring its defaults)
  - refuses while a persona in your file still points at it
  - downloaded files: if this installer downloaded them they are moved to the trash on the
    next ./install.sh (or right away with --delete-files); your own downloads are never touched
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

from .paths import Paths
from .registry import KINDS, builtin_ids, load
from .util import atomic_write

HEADER = "# Your models — merged on top of the built-in registry. Safe to edit, or use law add-model / remove-model.\n"
WEIGHT_EXT = (".safetensors", ".gguf", ".bin", ".npz")


class ModelCmdError(RuntimeError):
    pass


Fetch = Callable[[str, str | None], dict]


def hf_fetch(repo: str, token: str | None) -> dict:
    req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}?blobs=true")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ModelCmdError(f"{repo} does not exist on Hugging Face (check the spelling)") from e
        if e.code in (401, 403):
            raise ModelCmdError(f"{repo} is gated/private — accept its license on huggingface.co and put "
                                "HF_TOKEN=... in ~/.law/.env") from e
        raise ModelCmdError(f"Hugging Face answered HTTP {e.code} for {repo}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ModelCmdError(f"cannot reach Hugging Face ({e}) — check your internet connection") from e


def weights_gb(info: dict) -> float:
    sibs = info.get("siblings") or []
    for ext in WEIGHT_EXT:
        total = sum(s.get("size") or 0 for s in sibs if str(s.get("rfilename", "")).endswith(ext))
        if total:
            return round(total / 1024**3, 1)
    return 0.0


def guess_kind(info: dict) -> str:
    tag = (info.get("pipeline_tag") or "").lower()
    tags = {t.lower() for t in info.get("tags") or []}
    if tag in ("image-text-to-text", "image-to-text", "visual-question-answering") or "vision" in tags:
        return "vlm"
    if tag in ("feature-extraction", "sentence-similarity") or "sentence-transformers" in tags:
        return "embed"
    if tag == "text-to-speech":
        return "tts"
    if tag == "automatic-speech-recognition":
        return "stt"
    return "lm"


def guess_parsers(repo: str) -> tuple[str | None, str | None]:
    r = repo.lower()
    if "deepseek-r1" in r:
        return None, "qwen3"              # same as the registry's DeepSeek entries (the server has no deepseek_r1 parser)
    if "qwen3-coder" in r or "qwen3_coder" in r:
        return "qwen3_coder", None
    if re.search(r"qwen3\.[5-9]", r):        # Qwen3.5+ templates open <think> in the prompt
        return "qwen3_coder", "qwen3_5"
    if re.search(r"qwen3(?:[.\-_]|$)|qwq", r):
        return "qwen3", "qwen3"
    return None, None


def default_id(repo: str) -> str:
    name = repo.split("/")[-1].lower()
    name = re.sub(r"[-_.](mlx|4bit|8bit|6bit|3bit|dwq|instruct|it|ud)\b", "", name)
    return re.sub(r"[^a-z0-9.-]+", "-", name).strip("-.") or "model"


def _read(p: Paths) -> dict:
    if not p.user_models.exists():
        return {"models": []}
    data = yaml.safe_load(p.user_models.read_text()) or {}
    data.setdefault("models", [])
    data["models"] = data["models"] or []
    return data


def _write(p: Paths, data: dict) -> None:
    clean = {k: v for k, v in data.items() if v}
    atomic_write(p.user_models, HEADER + yaml.safe_dump(clean or {"models": []}, sort_keys=False))
    load(p)                                         # validate — raises on a broken result


def set_context(p: Paths, mid: str, context: int) -> None:
    """Record a measured context for a model in models.user.yaml (an override of only that field)."""
    data = _read(p)
    for m in data["models"]:
        if m.get("id") == mid:
            m["context"] = int(context)
            break
    else:
        data["models"].append({"id": mid, "context": int(context)})
    _write(p, data)


def add_model(p: Paths, repo: str, *, mid: str | None = None, kind: str | None = None,
              context: int | None = None, tool_parser: str | None = None, reasoning_parser: str | None = None,
              download: str = "manual", label: str | None = None, persona: str | None = None,
              force: bool = False, fetch: Fetch = hf_fetch, token: str | None = None) -> dict:
    if "/" not in repo or repo.count("/") != 1:
        raise ModelCmdError(f"'{repo}' is not a Hugging Face repo id (expected org/name)")
    if kind and kind not in KINDS:
        raise ModelCmdError(f"--kind must be one of {sorted(KINDS)}")
    reg = load(p)
    for m in reg.models.values():
        if m.repo == repo and m.id != mid:
            raise ModelCmdError(f"{repo} is already registered as '{m.id}'")
    mid = mid or default_id(repo)
    if mid in builtin_ids():
        raise ModelCmdError(f"'{mid}' is a built-in model id — choose another with --id")
    data = _read(p)
    existing = [m for m in data["models"] if m.get("id") == mid]
    if existing and not force:
        raise ModelCmdError(f"you already have a model '{mid}' — use --force to replace it, or --id")

    info = fetch(repo, token)
    gb = weights_gb(info)
    if gb <= 0:
        raise ModelCmdError(f"{repo} has no weight files (.safetensors/.gguf/.bin) — is it a model repo?")
    limit = reg.max_weights_gb
    if gb > limit and not force:
        raise ModelCmdError(f"{repo} needs ~{gb:g} GB of weights; this machine's limit is {limit:g} GB. "
                            "Pick a smaller / more quantised version (or --force to register it anyway; "
                            "it will still be refused at load time).")
    tp, rp = guess_parsers(repo)
    tags = {t.lower() for t in info.get("tags") or []}
    entry = {"id": mid, "repo": repo, "kind": kind or guess_kind(info), "weights_gb": gb,
             "context": context or 32768, "tool_parser": tool_parser if tool_parser is not None else tp,
             "reasoning_parser": reasoning_parser if reasoning_parser is not None else rp,
             "download": download, "license": (info.get("cardData") or {}).get("license") or "unknown",
             "verified": True, "label": label or f"added with law add-model ({repo})"}
    entry = {k: v for k, v in entry.items() if v is not None}
    data["models"] = [m for m in data["models"] if m.get("id") != mid] + [entry]
    if persona:
        if persona not in reg.personas:
            raise ModelCmdError(f"unknown persona '{persona}' — see `law models`")
        data.setdefault("personas", {})
        data["personas"] = data["personas"] or {}
        data["personas"][persona] = {**(data["personas"].get(persona) or {}), "model": mid}
    _write(p, data)
    warnings = []
    if "mlx" not in tags and info.get("library_name") != "mlx":
        warnings.append("this repo is not tagged as an MLX model — it may not load in mlx-openai-server. "
                        "Look for an mlx-community/… version.")
    if info.get("gated"):
        warnings.append("this repo is gated — make sure HF_TOKEN is in ~/.law/.env and the license is accepted.")
    return {"entry": entry, "warnings": warnings}


def remove_model(p: Paths, mid: str) -> dict:
    data = _read(p)
    entries = [m for m in data["models"] if m.get("id") == mid]
    if not entries:
        if mid in builtin_ids():
            raise ModelCmdError(f"'{mid}' is a built-in model. To stop downloading it, add\n"
                                f"  models:\n    - id: {mid}\n      download: manual\n"
                                "to ~/.law/config/models.user.yaml.")
        raise ModelCmdError(f"you have no model '{mid}' (see `law models`)")
    users = [n for n, pd in (data.get("personas") or {}).items() if (pd or {}).get("model") == mid]
    if users:
        raise ModelCmdError(f"persona(s) {', '.join(users)} still use '{mid}' in your models file — "
                            "point them at another model first")
    repo = entries[0].get("repo")
    data["models"] = [m for m in data["models"] if m.get("id") != mid]
    _write(p, data)
    return {"id": mid, "repo": repo, "was_override": mid in builtin_ids()}
