"""The web is every agent's default: every role is told how to search, and every OpenCode agent can run
`law search` and fetch pages (owner 2026-11-24)."""

from __future__ import annotations

from pathlib import Path

from law import roles

ROLES = Path(__file__).parents[2] / "settings" / "roles.yaml"


def test_every_role_is_told_how_to_search() -> None:
    r = roles.load(ROLES)
    for name in r.roles:
        prompt = r.system_prompt(name)
        assert "law search" in prompt and "web_search tool" in prompt, name
