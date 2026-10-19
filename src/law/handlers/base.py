"""Shared runtime for handlers: settings, LLM client and safe subprocess execution."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from law import registry, roles
from law.llm import LLM
from law.paths import LawPaths
from law.queue.worker import Cancelled, Context, Fatal


@dataclass
class Runtime:
    paths: LawPaths
    reg: registry.Registry
    roles: roles.Roles
    llm: LLM

    @classmethod
    def load(cls, paths: LawPaths) -> Runtime:
        reg = registry.load(paths.registry_file)
        return cls(paths, reg, roles.load(paths.settings / "roles.yaml"),
                   LLM(f"http://127.0.0.1:{reg.ports.llm_public}/v1"))

    def model_id(self, role_name: str) -> str:
        return self.reg.model_for_role(self.roles.get(role_name).model_role).id


async def run_cmd(ctx: Context, cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                  timeout_s: float = 3600, progress_every: int = 20,
                  describe: Callable[[str], str | None] | None = None) -> str:
    """Run a subprocess and kill it if the job is cancelled. Progress is either every Nth output line or, with
    `describe`, a readable summary of each line it recognises (e.g. an agent's tool calls)."""
    import os

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines: list[str] = []

    async def pump() -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            if describe is not None:
                summary = describe(line)
                if summary:
                    await ctx.progress(summary)
            elif len(lines) % progress_every == 0:
                await ctx.progress(line[-200:])

    async def watch_cancel() -> None:
        await ctx.cancel_event.wait()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    canceller = asyncio.create_task(watch_cancel())
    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        raise
    finally:
        canceller.cancel()
    if ctx.cancel_event.is_set():
        raise Cancelled()
    out = "\n".join(lines)
    if proc.returncode != 0:
        raise Fatal(f"{Path(cmd[0]).name} exited {proc.returncode}: {out[-1500:]}")
    return out
