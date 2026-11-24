"""Roles that may use the web are told how to search (OpenCode itself has only webfetch)."""

from __future__ import annotations

from pathlib import Path

from law import roles

ROLES = Path(__file__).parents[2] / "settings" / "roles.yaml"


def test_search_roles_get_law_search_and_others_do_not() -> None:
    r = roles.load(ROLES)
    with_search = [n for n, role in r.roles.items() if "search" in role.tools]
    without = [n for n, role in r.roles.items() if "search" not in role.tools]
    assert "writer" in with_search and without
    for name in with_search:
        assert "law search" in r.system_prompt(name)
    for name in without:
        assert "law search" not in r.system_prompt(name)
