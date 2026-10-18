"""Install order. Each step reconciles desired vs actual (install / repair / remove)."""
from __future__ import annotations

from ..engine import Step
from . import base, brew, docker, models, opencode, python_env, services, v1


def build_steps() -> list[Step]:
    return [
        *base.FIRST,          # folders, secrets file
        *v1.STEPS,            # carry over v1 settings, retire v1 (backup first)
        *brew.STEPS,          # system packages
        *python_env.STEPS,    # model-server Python runtime
        *models.STEPS,        # download missing models, prune unused
        *docker.STEPS,        # Colima + Open WebUI / SearXNG / Portainer
        *services.STEPS,      # worker + gateway + watchdog (+ telegram when configured)
        *opencode.STEPS,      # OpenCode provider pointing at the gateway (if OpenCode is used)
        *base.LAST,           # generated files, trash, version stamp
    ]
