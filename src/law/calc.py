"""`run_python`: the calculator models use instead of mental arithmetic.

Code runs in a separate scientific Python (~/LAW/runtime/calc: NumPy, SciPy, SymPy, pandas, pytest) as a child
process with a time limit, in a scratch directory under ~/LAW/cache/tmp.

Self-healing: when code fails on a missing module, `install_missing` adds the PyPI package to that Python, records
it in ~/LAW/state/calc-extra.txt (the calc-env install step reinstalls what is listed there) and the code runs again.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
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


MISSING = re.compile(r"ModuleNotFoundError: No module named '([A-Za-z_][A-Za-z0-9_]*)")
# import names whose PyPI package has another name
PACKAGE_FOR = {"sklearn": "scikit-learn", "yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python-headless",
               "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "skimage": "scikit-image",
               "Crypto": "pycryptodome", "docx": "python-docx", "pptx": "python-pptx"}


def missing_module(output: str) -> str | None:
    found = MISSING.search(output)
    return found.group(1) if found else None


async def install_missing(python: Path, module: str, env: dict[str, str], record: Path,
                          uv: str = "/opt/homebrew/bin/uv") -> str | None:
    """Install the package that provides `module` into the calculator's Python. Returns "name==version", or None
    if it could not be installed (unknown on PyPI, build failure)."""
    package = PACKAGE_FOR.get(module, module.replace("_", "-"))
    proc = await asyncio.create_subprocess_exec(uv, "pip", "install", "--python", str(python), package, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=600)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    if proc.returncode != 0:
        return None
    probe = await asyncio.create_subprocess_exec(
        str(python), "-I", "-c", f"import importlib.metadata as m; print(m.version({package!r}))",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await probe.communicate()
    pinned = f"{package}=={out.decode().strip()}" if probe.returncode == 0 else package
    lines = set(record.read_text().split()) if record.exists() else set()  # noqa: ASYNC240 - tiny local file
    lines = {x for x in lines if x.split("==")[0] != package} | {pinned}
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("\n".join(sorted(lines)) + "\n")  # noqa: ASYNC240
    return pinned
