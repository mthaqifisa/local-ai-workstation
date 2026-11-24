"""Typed view of settings/roles.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

# OpenCode has no web search, only webfetch: without this the agents guessed addresses (seen 2026-11-24: a research
# ticket tried pidm.com.my for an hour; `law search` finds the official pidm.gov.my page with the figure at once).
SEARCH_NOTE = ("Web search: `law search \"<query>\"` (LAW's own SearXNG) prints JSON results, each with a title, url "
               "and snippet. Search before you fetch: do not guess a site's address. Prefer the official source (the "
               "regulator's, the agency's or the company's own site), then reputable news, and open the page the "
               "search found to confirm the fact.")


# Every role that can run a command or browse has the web (owner 2026-11-24: "agent should have full web access so
# that data is updated and accurate"); only the translator, which reads files, has none.
WEB_TOOLS = {"search", "shell", "browser", "mac"}


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
        role = self.roles[name]
        search = f"\n\n{SEARCH_NOTE}" if WEB_TOOLS & set(role.tools) else ""
        return f"{role.prompt.strip()}{search}\n\n{self.house_standard.strip()}"

    def get(self, name: str) -> Role:
        if name not in self.roles:
            raise KeyError(f"unknown role {name!r}; known: {', '.join(sorted(self.roles))}")
        return self.roles[name]


def load(path: Path) -> Roles:
    return Roles.model_validate(yaml.safe_load(path.read_text()))
