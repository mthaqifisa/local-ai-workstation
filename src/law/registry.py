"""Typed view of settings/registry.yaml — the single source of truth (ADR-0008)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Colima(_Strict):
    cpus: int = Field(ge=1)
    memory_gb: int = Field(ge=2)
    disk_gb: int = Field(ge=20)


class Machine(_Strict):
    total_gb: int
    macos_reserve_gb: int
    colima: Colima
    model_budget_gb: int
    llm_ssd_cache_gb: int = Field(default=20, ge=1)
    llm_idle_unload_min: int = Field(default=5, ge=1)  # oMLX frees an idle model's memory after this

    @model_validator(mode="after")
    def _budget_fits(self) -> Machine:
        if self.model_budget_gb + self.macos_reserve_gb + self.colima.memory_gb > self.total_gb:
            raise ValueError("model_budget_gb + macos_reserve_gb + colima.memory_gb exceeds total_gb")
        return self


class Network(_Strict):
    bind: str
    loopback: str


class Ports(_Strict):
    llm_public: int
    llm_internal: int
    law_api: int
    open_webui: int
    homepage: int
    searxng: int
    comfyui: int
    valkey: int

    @model_validator(mode="after")
    def _unique(self) -> Ports:
        values = list(self.model_dump().values())
        if len(values) != len(set(values)):
            raise ValueError("ports must be unique")
        return self


class Service(_Strict):
    kind: Literal["launchd", "compose"]
    label: str | None = None
    requires_secret: str | None = None


class LlmModel(_Strict):
    id: str
    repo: str
    revision: str = Field(min_length=40, max_length=40)
    license: str
    disk_gb: float
    mem_gb: float
    lane: Literal["heavy", "light"]
    vision: bool = False
    context: int | None = None
    roles: list[str]


class FastVideo(_Strict):
    """A distilled few-step profile (e.g. Wan 2.2 Lightning): minutes per clip instead of most of an hour."""
    steps: int
    guidance: float
    guidance_2: float
    flow_shift: float
    loras: list[str]  # "repo@revision:path/in/repo.safetensors", pre-downloaded by `law install`
    lora_roles: list[str]

    def lora_args(self, hf_home: Path) -> list[str]:
        """Absolute paths of the pinned LoRA files in the shared HF cache (pre-downloaded by `law install`).
        mlx-gen does not resolve `repo:path` from the cache by itself ("will not download LoRA files during
        generation"), so it gets the files. A file that is missing falls back to the `repo:path` form."""
        out = []
        for lora in self.loras:
            ref, path = lora.split(":", 1)
            repo, _, rev = ref.partition("@")
            local = hf_home / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / rev / path
            out.append(str(local) if rev and local.is_file() else f"{repo}:{path}")
        return out


class Media(_Strict):
    tool: str
    model: str
    # The Hugging Face repo the tool actually downloads (mflux resolves short names like qwen-image-2.1).
    hf_repo: str | None = None
    # Companion repos the tool also downloads; `repo@revision` is pre-downloaded at that pin like hf_revision.
    hf_extra: list[str] = Field(default_factory=list)
    hf_revision: str | None = None  # pin; `law install` pre-downloads the repo at this revision
    prefetch: bool = True
    mem_gb: float
    lane: Literal["heavy", "light"]
    license: str
    quantize: int | None = None
    steps: int | None = None
    voice: str | None = None
    fast: FastVideo | None = None


class Tool(_Strict):
    name: str
    spec: str
    python: str
    check: str
    with_: list[str] = Field(default_factory=list, alias="with")
    own_venv: bool = False
    link: bool = True  # own_venv only: link `check` into runtime/bin (off when the name would clash)


class ComfyUI(_Strict):
    repo: str
    tag: str
    python: str


class TelegramSettings(_Strict):
    notify_sources: list[str] = Field(default_factory=lambda: ["cli", "api", "console"])


class Retention(_Strict):
    """How long LAW keeps history. Pruned every 6 hours by the worker and by `law reclaim`; editable in the
    Console (Storage). Live jobs are never pruned."""
    jobs_days: int = Field(default=14, ge=1)            # finished jobs and their progress logs
    agent_sessions_days: int = Field(default=14, ge=1)  # agents' full conversations (OpenCode sessions)
    logs_days: int = Field(default=14, ge=1)            # rotated logs in ~/LAW/logs
    trash_days: int = Field(default=7, ge=1)            # ~/LAW/trash


class Registry(_Strict):
    version: int
    machine: Machine
    network: Network
    ports: Ports
    services: dict[str, Service]
    llm_models: list[LlmModel]
    media: dict[str, Media] = {}
    tools: list[Tool] = []
    comfyui: ComfyUI | None = None
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    retention: Retention = Field(default_factory=Retention)

    @model_validator(mode="after")
    def _models_fit(self) -> Registry:
        ids = [m.id for m in self.llm_models]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate model id")
        for m in self.llm_models:
            if m.mem_gb > self.machine.model_budget_gb:
                raise ValueError(f"model {m.id} needs {m.mem_gb} GB > budget {self.machine.model_budget_gb}")
        return self

    def hf_repos_in_use(self) -> set[str]:
        """HF cache repos that `law reclaim` must keep."""
        repos = {m.hf_repo or m.model for m in self.media.values()}
        repos |= {r.split("@")[0] for m in self.media.values() for r in m.hf_extra}
        repos |= {r.split("@")[0] for m in self.media.values() if m.fast for r in m.fast.loras}
        return {r for r in repos if "/" in r}

    def model(self, model_id: str) -> LlmModel:
        for m in self.llm_models:
            if m.id == model_id:
                return m
        raise KeyError(model_id)

    def model_for_role(self, role: str) -> LlmModel:
        for m in self.llm_models:
            if role in m.roles:
                return m
        return self.model_for_role("default") if role != "default" else self.llm_models[0]


def load(path: Path) -> Registry:
    return Registry.model_validate(yaml.safe_load(path.read_text()))


def digest(path: Path) -> str:
    """Short hash of the registry; embedded in generated files so changes trigger reloads."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
