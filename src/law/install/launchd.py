"""launchd agents for LAW services: rendered from the registry, reloaded only when they change."""

from __future__ import annotations

import plistlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from law.paths import LawPaths
from law.sysapi import SysAPI

JDK = "/opt/homebrew/opt/openjdk@25"
BASE_PATH = f"{JDK}/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class Agent:
    label: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    keep_alive: bool = True
    cwd: Path | None = None
    nice: int = 0
    # True for processes that read settings/registry.yaml at start (api, worker, telegram): a registry
    # change must restart them. Others restart only when their own command line or environment changes.
    reads_registry: bool = False

    def render(self, paths: LawPaths, stamp: str) -> bytes:
        env = {"PATH": f"{paths.runtime / 'bin'}:{BASE_PATH}", "HOME": str(paths.user_home),
               "JAVA_HOME": f"{JDK}/libexec/openjdk.jdk/Contents/Home",
               "LAW_HOME": str(paths.home), "LAW_REPO": str(paths.repo), **self.env}
        if self.reads_registry:
            env["LAW_STAMP"] = stamp
        name = self.label.split(".")[-1]
        doc: dict[str, object] = {
            "Label": self.label,
            "ProgramArguments": self.args,
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": self.keep_alive,
            "ThrottleInterval": 10,
            "ProcessType": "Interactive",
            "StandardOutPath": str(paths.logs / f"{name}.log"),
            "StandardErrorPath": str(paths.logs / f"{name}.log"),
        }
        if self.cwd:
            doc["WorkingDirectory"] = str(self.cwd)
        if self.nice:
            doc["Nice"] = self.nice
        return plistlib.dumps(doc, sort_keys=True)


def stamp_of(plist: bytes) -> str | None:
    """The LAW_STAMP a rendered plist carries (None if absent or unreadable)."""
    try:
        doc = plistlib.loads(plist)
    except Exception:
        return None
    env = doc.get("EnvironmentVariables") or {}
    stamp = env.get("LAW_STAMP")
    return str(stamp) if stamp else None


def plist_path(paths: LawPaths, label: str) -> Path:
    return paths.launch_agents / f"{label}.plist"


def needs_update(paths: LawPaths, agent: Agent, stamp: str) -> bool:
    p = plist_path(paths, agent.label)
    return not p.exists() or p.read_bytes() != agent.render(paths, stamp)


def apply(paths: LawPaths, agent: Agent, stamp: str, sysapi: SysAPI, timeout_s: float = 30.0) -> None:
    """Write the plist and (re)load it. Idempotent: unchanged + loaded agents are left alone."""
    p = plist_path(paths, agent.label)
    changed = needs_update(paths, agent, stamp)
    if changed:
        p.parent.mkdir(parents=True, exist_ok=True)
        if sysapi.launchd_loaded(agent.label):
            sysapi.launchd_bootout(agent.label)
            # bootout is asynchronous: wait until launchd has really dropped the job before reloading.
            if not _wait(lambda: not sysapi.launchd_loaded(agent.label), timeout_s):
                raise RuntimeError(f"{agent.label} did not unload within {timeout_s}s")
        p.write_bytes(agent.render(paths, stamp))
    if not sysapi.launchd_loaded(agent.label):
        res = sysapi.launchd_bootstrap(p)
        if not res.ok and "already" not in res.out.lower():
            raise RuntimeError(f"launchctl bootstrap {agent.label} failed: {res.out}")
    if not sysapi.launchd_loaded(agent.label):
        raise RuntimeError(f"{agent.label} is not loaded after bootstrap")


def _wait(cond: Callable[[], bool], timeout_s: float, step_s: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step_s)
    return cond()


def remove(paths: LawPaths, label: str, sysapi: SysAPI) -> bool:
    p = plist_path(paths, label)
    if sysapi.launchd_loaded(label):
        sysapi.launchd_bootout(label)
    if p.exists():
        p.unlink()
        return True
    return False
