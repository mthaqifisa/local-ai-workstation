"""Ownership manifest — the installer only ever REMOVES what is recorded here.

~/.law/state/owned.json
{
  "brew":   ["ffmpeg", ...],                   formulae THIS installer installed
  "models": {"org/repo": {"complete": true}},  models this installer downloaded or adopted from v1
}
Things that were already on the Mac before (your own brew packages, models you
downloaded yourself) are never recorded, so they are never removed.
"""
from __future__ import annotations

import json
from pathlib import Path

from .util import atomic_write


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"brew": [], "models": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except json.JSONDecodeError:
                pass

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    # brew
    def own_brew(self, f: str) -> None:
        if f not in self.data["brew"]:
            self.data["brew"].append(f); self.save()

    def disown_brew(self, f: str) -> None:
        if f in self.data["brew"]:
            self.data["brew"].remove(f); self.save()

    # models
    def model(self, repo: str) -> dict | None:
        return self.data["models"].get(repo)

    def own_model(self, repo: str, complete: bool) -> None:
        self.data["models"][repo] = {"complete": complete}; self.save()

    def disown_model(self, repo: str) -> None:
        self.data["models"].pop(repo, None); self.save()


def load_manifest(paths) -> Manifest:
    return Manifest(paths.state / "owned.json")
