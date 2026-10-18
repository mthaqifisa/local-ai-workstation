"""Memory readings used by the model manager, the worker, the watchdog and `law status`.

pressure()  → "normal" | "warn" | "critical" | "unknown"
    macOS: the kernel's own verdict (kern.memorystatus_vm_pressure_level: 1/2/4) — the
    same signal Activity Monitor's memory-pressure graph uses. Elsewhere: % RAM used.
available_gb()  → RAM macOS can hand out right now without swapping much.
"""
from __future__ import annotations

import subprocess
import sys

import psutil

LEVELS = {"normal": 0, "unknown": 0, "warn": 1, "critical": 2}


def pressure() -> str:
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                               capture_output=True, text=True, timeout=5)
            return {"1": "normal", "2": "warn", "4": "critical"}.get(r.stdout.strip(), "unknown")
        except (OSError, subprocess.SubprocessError):
            return "unknown"
    pct = psutil.virtual_memory().percent
    return "critical" if pct >= 95 else "warn" if pct >= 85 else "normal"


def available_gb() -> float:
    return psutil.virtual_memory().available / 1024**3


def total_gb() -> float:
    return psutil.virtual_memory().total / 1024**3


def worse(a: str, b: str) -> str:
    return a if LEVELS.get(a, 0) >= LEVELS.get(b, 0) else b
