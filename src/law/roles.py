"""Typed view of settings/roles.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_role: str
    temperature: float
    reasoning: Literal["off", "low", "medium", "xhigh"] = "medium"
    tools: list[str]
    # Chat answers up to this many characters skip the independent review (ADR-0017); 0 reviews every answer.
    quick_reply_chars: int = 0
    summary: str
    prompt: str


class Roles(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int
    house_standard: str
    roles: dict[str, Role]

    def system_prompt(self, name: str) -> str:
        return f"{self.roles[name].prompt.strip()}\n\n{self.house_standard.strip()}"

    def get(self, name: str) -> Role:
        if name not in self.roles:
            raise KeyError(f"unknown role {name!r}; known: {', '.join(sorted(self.roles))}")
        return self.roles[name]


def load(path: Path) -> Roles:
    return Roles.model_validate(yaml.safe_load(path.read_text()))
