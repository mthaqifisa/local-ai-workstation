"""`law api` for Go: `go doc` run in the module, so the answer matches the versions in go.mod.

Queries: `http.Client` or `net/http.Client` (a type: its fields, constructors and methods), `http.Client.Do`
(one method), `http.Client Do` (the type's lines that mention Do), `github.com/gorilla/websocket.Upgrader`,
or a package path alone (its exported names).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from law.apilookup.common import ApiLookupError, clip, close_names, filter_lines, find_up, not_found

MARKERS = ("go.mod",)
EXPORT = re.compile(r"^(?:func|type|var|const)\s+(?:\([^)]*\)\s*)?([A-Z]\w*)", re.MULTILINE)
METHOD = re.compile(r"^func \([^)]*\)\s+([A-Z]\w*)", re.MULTILINE)


def split_query(query: str) -> tuple[str, str | None]:
    """`net/http.Client.Do` -> (`net/http`, `Client.Do`); `net/http` -> (`net/http`, None). The symbol starts at
    the first upper-case segment of the last path element (package paths may contain dots: github.com/...)."""
    path, slash, last = query.rpartition("/")
    parts = last.split(".")
    first = next((i for i, p in enumerate(parts) if p[:1].isupper()), None)
    if first is None or first == 0:
        return query, None
    return path + slash + ".".join(parts[:first]), ".".join(parts[first:])


def go_doc(args: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    tool = shutil.which("go")
    if tool is None:
        raise ApiLookupError("go not found: source the project's .law/env.sh")
    return subprocess.run([tool, "doc", *args], cwd=root, capture_output=True, text=True, timeout=300)


def names(pattern: re.Pattern[str], args: list[str], root: Path) -> list[str]:
    run = go_doc(args, root)
    return pattern.findall(run.stdout) if run.returncode == 0 else []


def declarations(text: str) -> str:
    """`go doc` output without comments and doc paragraphs (4-space indented), for member filtering."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//") and not ln.startswith("    "))


def lookup(query: str, member: str | None = None, *, start: Path) -> str:
    root = find_up(start, MARKERS)
    if root is None:
        raise ApiLookupError(f"no go.mod at or above {start}")
    package, symbol = split_query(query)
    run = go_doc([package, symbol] if symbol else ["-short", package], root)
    if run.returncode == 0:
        text = run.stdout.strip()
        return clip(filter_lines(declarations(text), member) if member else text)
    error = (run.stderr or run.stdout).strip()
    if "no such package" in error or "cannot find package" in error:
        return not_found("package", package, f"module {root.name} (go.mod) or the standard library", []) + (
            "\nIf it is a dependency, add it first: go get <module>@<version>")
    if symbol and "no method or field" in error:
        owner, _, method = symbol.rpartition(".")
        near = close_names(method, names(METHOD, [package, owner], root))
        return not_found("method or field", symbol, f"package {package}", [f"{owner}.{n}" for n in near])
    if symbol and "no symbol" in error:
        return not_found("symbol", symbol, f"package {package}", close_names(symbol, names(EXPORT, ["-short", package],
                                                                                           root)))
    raise ApiLookupError(error[-1500:])
