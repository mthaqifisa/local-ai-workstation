"""run_cmd: long output lines and always reaping the child process."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from law.handlers.base import run_cmd


def _ctx() -> Any:
    async def progress(_: str) -> None:
        return None

    return SimpleNamespace(cancel_event=asyncio.Event(), progress=progress)


def test_accepts_lines_longer_than_asyncio_default() -> None:
    script = "import sys; sys.stdout.write('x' * 300_000 + '\\nend\\n')"
    out = asyncio.run(run_cmd(_ctx(), [sys.executable, "-c", script]))
    assert out.splitlines()[0] == "x" * 300_000
    assert out.endswith("end")


def test_timeout_kills_the_child() -> None:
    async def go() -> None:
        with pytest.raises(TimeoutError):
            await run_cmd(_ctx(), [sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5)

    asyncio.run(asyncio.wait_for(go(), 10))


def test_a_command_without_work_is_stopped_as_stalled() -> None:
    from law.handlers.base import Stalled

    script = "import time\nprint('reading', flush=True)\ntime.sleep(30)"

    async def go() -> None:
        with pytest.raises(Stalled) as info:
            await run_cmd(_ctx(), [sys.executable, "-c", script], stall_s=0.4, is_work=lambda ln: "write" in ln)
        assert info.value.output == "reading"

    asyncio.run(asyncio.wait_for(go(), 10))


def test_steady_work_is_not_a_stall() -> None:
    script = "import time\nfor i in range(6):\n    print('write', i, flush=True); time.sleep(0.15)"
    out = asyncio.run(run_cmd(_ctx(), [sys.executable, "-c", script], stall_s=0.5, is_work=lambda ln: "write" in ln))
    assert out.splitlines()[-1] == "write 5"


def test_a_long_silent_generation_gets_grace_but_not_forever() -> None:
    from law.handlers.base import Stalled

    script = "import time\nprint('reading', flush=True)\ntime.sleep(30)"  # one action, then silence

    async def generating() -> bool:
        return True

    async def go() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(Stalled):
            await run_cmd(_ctx(), [sys.executable, "-c", script], stall_s=0.4, is_work=lambda ln: "write" in ln,
                          alive=generating)
        return loop.time() - start

    took = asyncio.run(asyncio.wait_for(go(), 10))
    assert 0.6 < took < 1.5  # past the 0.4 s stall time, stopped at the 1.8x grace limit


def test_a_read_loop_is_not_given_grace() -> None:
    from law.handlers.base import Stalled

    script = "import time\nfor i in range(100):\n    print('reading', i, flush=True); time.sleep(0.05)"

    async def generating() -> bool:
        return True

    async def go() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(Stalled):
            await run_cmd(_ctx(), [sys.executable, "-c", script], stall_s=0.4, is_work=lambda ln: "write" in ln,
                          alive=generating)
        return loop.time() - start

    assert asyncio.run(asyncio.wait_for(go(), 10)) < 0.7  # active but not saving: nudged on time
