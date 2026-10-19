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
