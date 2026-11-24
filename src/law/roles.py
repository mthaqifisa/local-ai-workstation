"""Typed view of settings/roles.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

# The web is every agent's default (owner 2026-11-24: "all agents should have web search capability ... it should be a
# default"). OpenCode has only webfetch, so without this the agents guessed addresses (a research ticket tried
# pidm.com.my for an hour; a search finds the official pidm.gov.my page with the figure at once).
SEARCH_NOTE = ("You have the web. Use it whenever a fact may have changed or you are not sure of it (figures, rules, "
               "versions, dates, names): search first, do not guess a site's address. In a shell, "
               "`law search \"<query>\"` (LAW's own SearXNG) prints JSON results with title, url and snippet; without "
               "a shell, use the web_search tool. Prefer the official source (the regulator's, the agency's or the "
               "company's own site), then reputable news, and open the page (webfetch, curl or fetch_page) to confirm "
               "the fact before you use it.")


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
        return f"{self.roles[name].prompt.strip()}\n\n{SEARCH_NOTE}\n\n{self.house_standard.strip()}"

    def get(self, name: str) -> Role:
        if name not in self.roles:
            raise KeyError(f"unknown role {name!r}; known: {', '.join(sorted(self.roles))}")
        return self.roles[name]


def load(path: Path) -> Roles:
    return Roles.model_validate(yaml.safe_load(path.read_text()))
