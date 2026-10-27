"""Shared runtime for handlers: settings, LLM client and safe subprocess execution."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
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


STALL_GRACE = 1.8  # a long generation may run to 1.8 x the stall time (25 -> 45 min)

class Steered(Exception):
    """`steer` asked to stop the command and redirect it (e.g. an agent reading library internals). Carries the
    output so far and the message to resume with."""

    def __init__(self, output: str, message: str) -> None:
        super().__init__(message)
        self.output = output
        self.message = message


# Agents emit JSON events that embed whole files; asyncio's 64 KiB default line limit is too small.
MAX_LINE_BYTES = 64 * 1024 * 1024


async def run_cmd(ctx: Context, cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                  timeout_s: float = 3600, progress_every: int = 20,
                  describe: Callable[[str], str | None] | None = None,
                  stall_s: float | None = None, is_work: Callable[[str], bool] | None = None,
                  alive: Callable[[], Awaitable[bool]] | None = None,
                  steer: Callable[[str], str | None] | None = None,
                  changed_since: Callable[[float], bool] | None = None) -> str:
    """Run a subprocess and kill it if the job is cancelled. Progress is either every Nth output line or, with
    `describe`, a readable summary of each line it recognises (e.g. an agent's tool calls). With `stall_s`, a
    command that emits no line accepted by `is_work` for that long is stopped and `Stalled` is raised, unless
    `alive()` says the model is generating: then the reply in progress may finish (it often is the file being
    written), up to STALL_GRACE times the stall time; the stop comes after that reply if it saved nothing.
    `steer` sees each described action; a message from it stops the command and raises `Steered` with it."""
    import os
    import time

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=MAX_LINE_BYTES)
    lines: list[str] = []
    last_work = last_any = time.monotonic()
    stalled = False
    steered: str | None = None

    async def pump() -> None:
        nonlocal last_work, last_any, steered
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            if is_work is not None and is_work(line):
                last_work = time.monotonic()
            if describe is not None:
                summary = describe(line)
                if summary:
                    last_any = time.monotonic()  # an action (read, command, edit); the model's prose is not
                    await ctx.progress(summary)
                    if steer is not None and steered is None and (message := steer(summary)):
                        steered = message
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
            else:
                last_any = time.monotonic()
                if len(lines) % progress_every == 0:
                    await ctx.progress(line[-200:])

    async def watch_cancel() -> None:
        await ctx.cancel_event.wait()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    async def watch_stall() -> None:
        nonlocal stalled, last_work
        assert stall_s is not None
        reply_from: float | None = None  # the last action before the reply we let finish
        while True:
            await asyncio.sleep(min(stall_s / 4, 30.0))
            now = time.monotonic()
            # a file changed on disk by a shell command is work too (the agent's own edit events are not the
            # only way it saves: seen 2026-10-28, 25 min of real edits through python scripts struck as a stall)
            if now - last_work >= stall_s and changed_since is not None and \
                    changed_since(time.time() - (now - last_work)):
                last_work = now
            if now - last_work < stall_s:
                reply_from = None
                continue
            # Past the stall time while the model generates: let that one reply finish (a new action marks its
            # end), within the grace limit. A read loop is stopped right after its current reply.
            if now - last_work < stall_s * STALL_GRACE and alive is not None and await alive():
                if reply_from is None:
                    reply_from = last_any
                if last_any == reply_from:
                    continue
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
    if steered is not None:
        raise Steered(out, steered)
    if stalled:
        raise Stalled(out, time.monotonic() - last_work)
    if proc.returncode != 0:
        raise Fatal(f"{Path(cmd[0]).name} exited {proc.returncode}: {out[-1500:]}")
    return out
