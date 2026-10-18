"""All filesystem locations in one place. Everything honours $LAW_HOME / $HOME so
tests can run against a throw-away home directory."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    home: Path

    # ── v2 ───────────────────────────────────────────────────────────────
    @property
    def law(self) -> Path:
        return Path(os.environ.get("LAW_HOME", self.home / ".law"))

    @property
    def env_file(self) -> Path:
        return self.law / ".env"

    @property
    def config(self) -> Path:
        return self.law / "config"

    @property
    def user_models(self) -> Path:
        return self.config / "models.user.yaml"

    @property
    def user_personas(self) -> Path:
        return self.config / "personas.user.json"

    @property
    def state(self) -> Path:
        return self.law / "state"

    @property
    def install_state(self) -> Path:
        return self.state / "install.json"

    @property
    def run(self) -> Path:
        return self.law / "run"

    @property
    def lock(self) -> Path:
        return self.run / "law.lock"

    @property
    def logs(self) -> Path:
        return self.law / "logs"

    @property
    def backups(self) -> Path:
        return self.law / "backups"

    @property
    def generated(self) -> Path:
        return self.law / "generated"

    @property
    def runtime(self) -> Path:       # model-server Python runtime
        return self.law / "runtime"

    @property
    def trash(self) -> Path:         # removed items wait here 7 days
        return self.law / "trash"

    @property
    def queue_db(self) -> Path:      # job queue (SQLite)
        return self.state / "queue.db"

    @property
    def agent_db(self) -> Path:      # per-project agent store: specs, handoffs, runs, memory (SQLite)
        return self.state / "agent.db"

    @property
    def undo_dir(self) -> Path:      # per-project undo git histories (one subfolder each)
        return self.state / "undo"

    @property
    def probes(self) -> Path:        # `law probe` results
        return self.state / "probes.json"

    @property
    def searxng(self) -> Path:
        return self.law / "searxng"

    # ── shared user data (never deleted) ────────────────────────────────
    @property
    def workspace(self) -> Path:
        return self.home / "MLX-AI"

    @property
    def source(self) -> Path:
        return self.workspace / "source"

    @property
    def outputs(self) -> Path:
        return self.workspace / "outputs"

    @property
    def hf_cache(self) -> Path:
        base = os.environ.get("HF_HUB_CACHE")
        if base:
            return Path(base)
        hf_home = os.environ.get("HF_HOME")
        if hf_home:
            return Path(hf_home) / "hub"
        return self.home / ".cache" / "huggingface" / "hub"

    @property
    def launch_agents(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    # ── v1 (read-only, for migration/cleanup) ───────────────────────────
    @property
    def v1(self) -> Path:
        return self.home / ".mlx-ai-workstation"


def get_paths() -> Paths:
    return Paths(home=Path(os.environ.get("HOME", str(Path.home()))))
