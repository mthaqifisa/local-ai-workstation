"""Roles that may use the web are told how to search (OpenCode itself has only webfetch)."""

from __future__ import annotations

from pathlib import Path

from law import roles

ROLES = Path(__file__).parents[2] / "settings" / "roles.yaml"


def test_every_role_that_runs_commands_gets_the_web() -> None:
    r = roles.load(ROLES)
    with_search = [n for n, role in r.roles.items() if roles.WEB_TOOLS & set(role.tools)]
    without = [n for n, role in r.roles.items() if not roles.WEB_TOOLS & set(role.tools)]
    assert {"writer", "editor", "qa", "analyst", "reviewer", "designer", "coder"} <= set(with_search)
    assert without == ["translator"]
    for name in with_search:
        assert "law search" in r.system_prompt(name)
    for name in without:
        assert "law search" not in r.system_prompt(name)
