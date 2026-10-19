"""Where law's disk space goes — shared by `law status`, `law reclaim` and the dashboard.

Models are big and the Mac's disk fills quietly (a model-server bug once leaked ~300 GB of
prompt caches into the system temp folder), so the usage is shown, not guessed."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from .paths import Paths

LOW_GB = 50            # warn below this much free disk
CRITICAL_GB = 15       # refuse model downloads / warn on loads below this

_CACHE: dict = {}


def dir_bytes(d: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(d, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_blocks * 512   # allocated, not apparent (sparse VM disks)
            except OSError:
                pass
    return total


def prompt_cache_leftovers() -> list[Path]:
    root = Path(tempfile.gettempdir())
    return [d for d in root.glob("mlx-openai-prompt-cache-*") if d.is_dir()]


def usage(p: Paths, max_age_s: float = 120) -> dict:
    """{'free_gb','total_gb','items':[{'name','path','gb','note'}], 'level': ok|low|critical}.
    Cached for max_age_s (walking the model cache takes a moment)."""
    key = str(p.home)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < max_age_s:
        return hit[1]
    items = []

    def add(name: str, path: Path, note: str = "") -> None:
        if path.exists():
            items.append({"name": name, "path": str(path), "gb": round(dir_bytes(path) / 1024**3, 1),
                          "note": note})
    add("models (Hugging Face cache)", p.hf_cache, "law remove-model <id> to drop one")
    add("model-server runtime", p.runtime, "Python + MLX packages")
    add("law state (queue, agent store, undo)", p.state)
    add("law logs", p.logs, "rotated by the watchdog")
    add("law trash", p.trash, "law reclaim --trash")
    add("model temp / prompt cache", p.run / "model-tmp", "wiped whenever the model stops")
    add("generated media", p.outputs, "law reclaim --media DAYS")
    add("Docker VM (Colima)", p.home / ".colima", "law reclaim trims it")
    add("ACE-Step music", p.home / "ace-step")
    left = prompt_cache_leftovers()
    if left:
        items.append({"name": f"LEAKED prompt caches ({len(left)})", "path": str(left[0].parent),
                      "gb": round(sum(dir_bytes(d) for d in left) / 1024**3, 1), "note": "law reclaim removes them"})
    try:
        du = shutil.disk_usage("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else p.home)
        free, total = du.free / 1024**3, du.total / 1024**3
    except OSError:
        free, total = 0.0, 0.0
    level = "critical" if free < CRITICAL_GB else "low" if free < LOW_GB else "ok"
    res = {"free_gb": round(free), "total_gb": round(total), "level": level, "items": items}
    _CACHE[key] = (time.time(), res)
    return res
