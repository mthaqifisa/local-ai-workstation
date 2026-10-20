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


class Stalled(Exception):
    """The command produced no work (per `is_work`) for `stall_s` seconds and was stopped. Carries its output."""

    def __init__(self, output: str, idle_s: float) -> None:
        super().__init__(f"no progress for {idle_s / 60:.0f} min")
        self.output = output
        self.idle_s = idle_s


# Agents emit JSON events that embed whole files; asyncio's 64 KiB default line limit is too small.
MAX_LINE_BYTES = 64 * 1024 * 1024


async def run_cmd(ctx: Context, cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                  timeout_s: float = 3600, progress_every: int = 20,
                  describe: Callable[[str], str | None] | None = None,
                  stall_s: float | None = None, is_work: Callable[[str], bool] | None = None) -> str:
    """Run a subprocess and kill it if the job is cancelled. Progress is either every Nth output line or, with
    `describe`, a readable summary of each line it recognises (e.g. an agent's tool calls). With `stall_s`, a
    command that emits no line accepted by `is_work` for that long is stopped and `Stalled` is raised."""
    import os
    import time

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=MAX_LINE_BYTES)
    lines: list[str] = []
    last_work = time.monotonic()
    stalled = False

    async def pump() -> None:
        nonlocal last_work
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            if is_work is not None and is_work(line):
                last_work = time.monotonic()
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

    async def watch_stall() -> None:
        nonlocal stalled
        assert stall_s is not None
        while True:
            await asyncio.sleep(min(stall_s / 4, 30.0))
            if time.monotonic() - last_work >= stall_s:
                stalled = True
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return

    canceller = asyncio.create_task(watch_cancel())
    staller = asyncio.create_task(watch_stall()) if stall_s else None
    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout_s)
    finally:
        canceller.cancel()
        if staller is not None:
            staller.cancel()
        if proc.returncode is None:  # timeout, cancel or a reader error: never leave the child running
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if ctx.cancel_event.is_set():
        raise Cancelled()
    out = "\n".join(lines)
    if stalled:
        raise Stalled(out, time.monotonic() - last_work)
    if proc.returncode != 0:
        raise Fatal(f"{Path(cmd[0]).name} exited {proc.returncode}: {out[-1500:]}")
    return out
