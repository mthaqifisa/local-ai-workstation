"""`run_python`: the calculator models use instead of mental arithmetic.

Code runs in a separate scientific Python (~/LAW/runtime/calc: NumPy, SciPy, SymPy, pandas) as a child
process with a time limit, in a scratch directory under ~/LAW/cache/tmp.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from pathlib import Path
from typing import Any

TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": ("Run Python 3 code and return what it prints. Use it for every calculation, statistic, "
                        "date or unit conversion, and to run code you wrote with its tests before you show it. "
                        "numpy, scipy, sympy, pandas, pytest and decimal are available; the working folder is "
                        "writable (write test_x.py, then pytest.main(['-q', 'test_x.py'])). print() what you need."),
        "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "Python code"}},
                       "required": ["code"]},
    },
}


async def run_python(python: Path, scratch: Path, code: str, timeout_s: float = 60) -> str:
    scratch.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one local mkdir, negligible
    with tempfile.TemporaryDirectory(dir=scratch) as work:
        proc = await asyncio.create_subprocess_exec(
            str(python), "-I", "-c", code, cwd=work,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return f"error: timed out after {timeout_s:.0f}s"
    text = out.decode(errors="replace").strip()
    status = "" if proc.returncode == 0 else f"(exit {proc.returncode})\n"
    return (status + text)[-6000:] or "(no output; use print())"
