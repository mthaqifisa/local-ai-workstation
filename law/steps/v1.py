"""Retire the v1 stack (mlx-setup.sh) — automatically, because v2 replaces it.

1. migrate (always, never overwrites): secrets → ~/.law/.env, custom models →
   models.user.yaml, personas.json → personas.user.json
2. adopt the models v1 downloaded, so models no longer in the v2 registry get pruned
   (moved to ~/.law/trash, auto-emptied after 7 days)
3. back up v1 config (not its venv) → ~/.law/backups/v1-<time>
4. stop + remove every com.mlxstack.* service, the mlx-agent shim, ~/.mlx-ai-workstation

Kept: HF model cache (models still in the registry), ~/MLX-AI, ~/ace-step, Docker
volumes (Open WebUI chats). `./install.sh --keep-v1` skips steps 2–4.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import yaml

from ..engine import Action, Ctx, Step
from ..manifest import load_manifest
from ..paths import Paths
from ..registry import load
from ..util import atomic_write, copy_if_absent, is_macos, merge_env, read_env

PREFIX = "com.mlxstack."
ENV_KEYS = ["HF_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID"]   # DASHBOARD_PASSWORD dropped: no auth
SKIP_BACKUP = {".venv", "langfuse", "__pycache__", "searxng"}
TYPE_TO_KIND = {"lm": "lm", "llm": "lm", "multimodal": "vlm", "vlm": "vlm",
                "embeddings": "embed", "embedding": "embed", "embed": "embed"}

# every repo the v1 installer itself could download (from mlx-setup.sh)
V1_REPOS = [
    "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit", "mlx-community/Qwen3.8-27B-4bit",
    "mlx-community/Qwen3.6-27B-8bit", "mlx-community/DeepSeek-R1-Distill-Qwen-32B-4bit",
    "mlx-community/DeepSeek-R1-Distill-Llama-70B-4bit", "mlx-community/Qwen3-Embedding-8B-4bit-DWQ",
    "mlx-community/Qwen3-Coder-Next-4bit", "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
    "mlx-community/Devstral-Small-2-4bit", "richardyoung/Kimi-K2-Instruct-0905-MLX-4bit",
    "mlx-community/Qwen3-VL-8B-Instruct-4bit", "mlx-community/gemma-4-12B-4bit",
    "mlx-community/gemma-4-12B-it-4bit", "mlx-community/gemma-4-31B-4bit",
    "mlx-community/gemma-3-12b-it-4bit", "mlx-community/Qwen3-8B-4bit",
    "prince-canuma/Kokoro-82M",
]


def cache_dir(p: Paths, repo: str) -> Path:
    return p.hf_cache / ("models--" + repo.replace("/", "--"))


def _plists(p: Paths) -> list[Path]:
    return sorted(p.launch_agents.glob(PREFIX + "*.plist")) if p.launch_agents.is_dir() else []


def _shim(p: Paths) -> Path:
    return p.home / ".local" / "bin" / "mlx-agent"


def _present(p: Paths) -> bool:
    return p.v1.exists() or bool(_plists(p)) or _shim(p).exists()


# ── migration ───────────────────────────────────────────────────────────
def parse_tsv(tsv: Path) -> list[dict]:
    rows = []
    for line in tsv.read_text(errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3 or "/" not in parts[1]:
            continue
        rows.append({"role": parts[0].strip(), "repo": parts[1].strip(), "type": parts[2].strip(),
                     "tool_parser": (parts[3].strip() or None) if len(parts) > 3 else None,
                     "reasoning_parser": (parts[4].strip() or None) if len(parts) > 4 else None})
    return rows


def _size_gb(p: Paths, repo: str) -> float:
    total = 0
    for root, _, files in os.walk(cache_dir(p, repo) / "blobs"):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return round(total / 1024**3, 1)


def _missing_env(p: Paths) -> dict[str, str]:
    src, cur = read_env(p.v1 / ".env"), read_env(p.env_file)
    return {k: src[k] for k in ENV_KEYS if src.get(k) and not cur.get(k)}


def _user_repos(p: Paths) -> set[str]:
    if not p.user_models.exists():
        return set()
    return {m.get("repo") for m in (yaml.safe_load(p.user_models.read_text()) or {}).get("models", []) or []}


def _missing_models(p: Paths) -> list[dict]:
    tsv = p.v1 / "models.custom.tsv"
    if not tsv.exists():
        return []
    known = {m.repo for m in load().models.values()} | _user_repos(p)
    return [r for r in parse_tsv(tsv) if r["repo"] not in known]


def _add_user_models(p: Paths, rows: list[dict]) -> None:
    data = {"models": []}
    if p.user_models.exists():
        data = yaml.safe_load(p.user_models.read_text()) or {"models": []}
        data.setdefault("models", [])
    ids = {m.get("id") for m in data["models"]} | set(load().models)
    for r in rows:
        mid = r["role"] or r["repo"].split("/")[-1].lower()
        base, n = mid, 2
        while mid in ids:
            mid, n = f"{base}-{n}", n + 1
        ids.add(mid)
        data["models"].append({
            "id": mid, "repo": r["repo"], "kind": TYPE_TO_KIND.get(r["type"].lower(), "lm"),
            "weights_gb": _size_gb(p, r["repo"]), "tool_parser": r["tool_parser"],
            "reasoning_parser": r["reasoning_parser"], "download": "manual", "verified": False,
            "label": f"migrated from v1 ({r['role']})"})
    atomic_write(p.user_models, "# Your models — merged on top of the built-in registry. Safe to edit.\n"
                 + yaml.safe_dump(data, sort_keys=False))


def plan_migrate(ctx: Ctx) -> list[Action]:
    p = ctx.paths
    if not p.v1.is_dir():
        return []
    acts = []
    env = _missing_env(p)
    if env:
        acts.append(Action("add", "secrets from v1: " + ", ".join(env), lambda: merge_env(p.env_file, env)))
    rows = _missing_models(p)
    if rows:
        acts.append(Action("add", "your v1 custom models: " + ", ".join(r["repo"] for r in rows),
                           lambda: _add_user_models(p, rows)))
    if (p.v1 / "personas.json").exists() and not p.user_personas.exists():
        acts.append(Action("add", "your v1 personas → personas.user.json",
                           lambda: copy_if_absent(p.v1 / "personas.json", p.user_personas)))
    return acts


# ── retirement ──────────────────────────────────────────────────────────
def _adopt_models(p: Paths) -> None:
    man = load_manifest(p)
    for repo in V1_REPOS:
        if cache_dir(p, repo).is_dir() and man.model(repo) is None:
            man.own_model(repo, complete=False)      # re-verified by the models step


def _backup(p: Paths) -> None:
    base = p.backups / f"v1-{time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = base, 2
    while dest.exists():
        dest, n = Path(f"{base}-{n}"), n + 1
    (dest / "misc").mkdir(parents=True)
    if p.v1.is_dir():
        for item in p.v1.iterdir():
            if item.name in SKIP_BACKUP:
                continue
            t = dest / "mlx-ai-workstation" / item.name
            t.parent.mkdir(parents=True, exist_ok=True)
            if item.is_dir() and not item.is_symlink():
                shutil.copytree(item, t, symlinks=True)
            else:
                shutil.copy2(item, t, follow_symlinks=False)
    for f in _plists(p) + ([_shim(p)] if _shim(p).exists() else []):
        shutil.copy2(f, dest / "misc" / f.name)
    oc = p.home / ".config" / "opencode" / "config.json"
    if oc.exists():
        shutil.copy2(oc, dest / "misc" / "opencode-config.json")
    (dest / "MANIFEST.json").write_text(json.dumps({"created": time.ctime(),
                                                    "services": [f.name for f in _plists(p)]}, indent=2))


def _remove_services(ctx: Ctx) -> None:
    for f in _plists(ctx.paths):
        if is_macos():
            ctx.sys.launchctl_bootout(f.stem)
        f.unlink(missing_ok=True)


def plan_retire(ctx: Ctx) -> list[Action]:
    p = ctx.paths
    if ctx.keep_v1 or not _present(p):
        return []
    acts = [Action("add", "adopt v1-downloaded models (unneeded ones get pruned)", lambda: _adopt_models(p)),
            Action("add", f"back up v1 config → {p.backups}/v1-<time>", lambda: _backup(p))]
    pl = _plists(p)
    if pl:
        acts.append(Action("remove", "v1 services: " + ", ".join(f.stem[len(PREFIX):] for f in pl),
                           lambda: _remove_services(ctx)))
    if _shim(p).exists():
        acts.append(Action("remove", str(_shim(p)), lambda: _shim(p).unlink(missing_ok=True)))
    if p.v1.exists():
        acts.append(Action("remove", str(p.v1), lambda: shutil.rmtree(p.v1, ignore_errors=True)))
    return acts


STEPS = [
    Step("v1-migrate", "Carry over your v1 settings", plan_migrate),
    Step("v1-retire", "Retire the v1 stack (backup first)", plan_retire),
]
