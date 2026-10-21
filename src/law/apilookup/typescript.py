"""`law api` for TypeScript/JavaScript (Node, Angular, React, React Native): declarations from the installed
packages' .d.ts files, resolved by the TypeScript compiler exactly as the project's build resolves them.

Queries: `@angular/common/http:HttpClient` (module:export), `HttpClient` alone (searched in the direct dependencies
and their entry points), with an optional member filter: `law api @angular/core:Component standalone`.
The compiler is the project's own node_modules/typescript when it has the JavaScript compiler API (5.x/6.x),
else LAW's pinned one (~/LAW/runtime/api-tools, the api-tools install step): TypeScript 7 is a native binary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

from law.apilookup.common import ApiLookupError, clip, close_names, find_up, not_found
from law.paths import LawPaths

MARKERS = ("package.json",)
SCRIPT = Path(__file__).with_name("ts_api.mjs")


def has_compiler_api(lib: Path) -> bool:
    """TypeScript before 7 ships the JavaScript compiler API (lib/typescript.js)."""
    manifest = lib / "package.json"
    if not manifest.is_file() or not (lib / "lib" / "typescript.js").is_file():
        return False
    major = str(json.loads(manifest.read_text()).get("version", "0")).split(".")[0]
    return major.isdigit() and int(major) < 7


def typescript_lib(root: Path, own: Path | None = None) -> Path:
    for d in (root, *root.parents):
        lib = d / "node_modules" / "typescript"
        if has_compiler_api(lib):
            return lib
    law = own or LawPaths.from_env().runtime / "api-tools" / "node_modules" / "typescript"
    if has_compiler_api(law):
        return law
    raise ApiLookupError("no TypeScript compiler API: run `law install` (api-tools step)")


class Found(TypedDict):
    module: str
    file: str
    text: str


class Answer(TypedDict, total=False):
    found: list[Found]
    names: list[str]
    missing: list[str]
    error: str


def run_script(root: Path, query: str, member: str | None) -> Answer:
    node = shutil.which("node")
    if node is None:
        raise ApiLookupError("node not found: source the project's .law/env.sh")
    if not (root / "node_modules").is_dir():
        raise ApiLookupError(f"{root / 'node_modules'} is missing: run npm install first")
    cmd = [node, str(SCRIPT), str(typescript_lib(root)), str(root), query, *([member] if member else [])]
    run = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=300)
    try:
        data: Answer = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise ApiLookupError(f"lookup failed: {(run.stderr or run.stdout)[-1500:]}") from exc
    if data.get("error"):
        raise ApiLookupError(f"lookup failed: {data.get('error', '')[-1500:]}")
    return data


def lookup(query: str, member: str | None = None, *, start: Path) -> str:
    root = find_up(start, MARKERS)
    if root is None:
        raise ApiLookupError(f"no package.json at or above {start}")
    data = run_script(root, query, member)
    module, _, name = query.rpartition(":") if ":" in query.lstrip("@") else ("", "", query)
    found = data.get("found", [])
    if module and module in data.get("missing", []):
        return not_found("module", module, f"{root.name}'s node_modules", []) + (
            f"\nInstall it (npm install {module}) or, for a package without types, its @types package.")
    if not found:
        where = f"module {module}" if module else f"the dependencies of {root.name} (package.json)"
        return not_found("export", name, where, close_names(name, data.get("names", [])))
    seen: set[str] = set()
    blocks = []
    for f in found:
        if f["text"] in seen:
            continue
        seen.add(f["text"])
        blocks.append(f"// {f['module']}  ({f['file']})\n{f['text']}")
    return clip("\n\n".join(blocks))
