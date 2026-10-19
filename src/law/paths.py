"""Filesystem layout (ADR-0005). Every path LAW touches is derived from here."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _repo_default() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LawPaths:
    """Resolved locations: `repo` holds code and portable state, `home` holds machine-local data."""

    repo: Path
    home: Path
    user_home: Path

    @classmethod
    def from_env(cls) -> LawPaths:
        user_home = Path(os.environ.get("HOME", str(Path.home())))
        home = Path(os.environ.get("LAW_HOME", str(user_home / "LAW")))
        repo = Path(os.environ.get("LAW_REPO", str(_repo_default())))
        return cls(repo=repo, home=home, user_home=user_home)

    # ---- portable state (git) ----
    @property
    def settings(self) -> Path:
        return self.repo / "settings"

    @property
    def knowledge(self) -> Path:
        return self.repo / "knowledge"

    @property
    def trained(self) -> Path:
        return self.repo / "trained"

    @property
    def templates(self) -> Path:
        return self.repo / "templates"

    @property
    def registry_file(self) -> Path:
        return self.settings / "registry.yaml"

    # ---- machine-local (~/LAW) ----
    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def state(self) -> Path:
        return self.home / "state"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def models(self) -> Path:
        return self.home / "models"

    @property
    def llm_models(self) -> Path:
        return self.models / "llm"

    @property
    def runtime(self) -> Path:
        return self.home / "runtime"

    @property
    def outputs(self) -> Path:
        return self.home / "outputs"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def trash(self) -> Path:
        return self.home / "trash"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def tmp(self) -> Path:
        return self.cache / "tmp"

    @property
    def env_file(self) -> Path:
        return self.state / ".env"

    @property
    def env_sh(self) -> Path:
        return self.config / "env.sh"

    @property
    def launch_agents(self) -> Path:
        return self.user_home / "Library" / "LaunchAgents"

    @property
    def projects(self) -> Path:
        """Where agent-created code projects go (code lives only in ~/SourceCode)."""
        return self.user_home / "SourceCode"

    def skeleton(self) -> list[Path]:
        """Directories `law install` guarantees exist."""
        return [
            self.config, self.state, self.data, self.models, self.llm_models, self.models / "hf",
            self.models / "comfy", self.models / "other", self.runtime, self.home / "colima",
            self.home / "docker", self.outputs / "images", self.outputs / "video", self.outputs / "audio",
            self.outputs / "docs", self.outputs / "test-artifacts", self.logs, self.home / "backups",
            self.trash, self.home / "archive", self.cache / "uv", self.cache / "pip", self.cache / "npm",
            self.cache / "playwright", self.cache / "torch", self.tmp, self.data / "valkey",
        ]
