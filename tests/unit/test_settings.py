"""The committed settings must always load and stay internally consistent."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from law import registry, roles

REPO = Path(__file__).resolve().parents[2]


def test_registry_loads_and_models_fit_budget():
    reg = registry.load(REPO / "settings" / "registry.yaml")
    assert reg.machine.model_budget_gb + reg.machine.macos_reserve_gb + reg.machine.colima.memory_gb <= 64
    for m in reg.llm_models:
        assert m.mem_gb <= reg.machine.model_budget_gb
        assert len(m.revision) == 40


def test_every_role_maps_to_a_model():
    reg = registry.load(REPO / "settings" / "registry.yaml")
    rl = roles.load(REPO / "settings" / "roles.yaml")
    for name, role in rl.roles.items():
        assert reg.model_for_role(role.model_role).id, name


def test_judge_is_a_different_family_from_generators():
    reg = registry.load(REPO / "settings" / "registry.yaml")
    judge = reg.model_for_role("judge").repo.lower()
    for role in ("default", "coder", "writer"):
        gen = reg.model_for_role(role).repo.lower()
        assert ("qwen" in gen) != ("qwen" in judge), (role, gen, judge)


def test_duplicate_ports_rejected(tmp_path):
    data = yaml.safe_load((REPO / "settings" / "registry.yaml").read_text())
    data["ports"]["homepage"] = data["ports"]["open_webui"]
    f = tmp_path / "r.yaml"
    f.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        registry.load(f)


def test_model_over_budget_rejected(tmp_path):
    data = yaml.safe_load((REPO / "settings" / "registry.yaml").read_text())
    data["llm_models"][0]["mem_gb"] = 999
    f = tmp_path / "r.yaml"
    f.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        registry.load(f)


def test_unknown_role_error_lists_known_roles():
    rl = roles.load(REPO / "settings" / "roles.yaml")
    with pytest.raises(KeyError, match="known"):
        rl.get("nope")
