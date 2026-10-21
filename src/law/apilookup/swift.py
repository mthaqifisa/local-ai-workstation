"""`law api` for Swift (iOS, watchOS and macOS apps, Swift packages): declarations from the installed Xcode SDK
and from Swift Package Manager dependency sources, so the answer matches this Xcode.

The modules the project imports are read as the compiler sees them, Objective-C frameworks included
(`swift-synthesize-interface`, cached per SDK version under ~/LAW/cache/apilookup); other SDK modules fall back to
their .swiftinterface files (Swift-only, no cache needed).

Queries: `NavigationStack` (searched in the modules the project imports, then the whole SDK), `SwiftUI.List`
(one module), with an optional member filter: `law api View navigationTitle` (also searches `extension View`
blocks, where SwiftUI defines its modifiers). The SDK is iOS Simulator unless the project targets another
platform (project.yml / Package.swift mention watchOS or macOS).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from law.apilookup.common import ApiLookupError, clip, close_names, find_up, not_found
from law.paths import LawPaths

MARKERS = ("Package.swift", "project.yml", "*.xcodeproj", "*.xcworkspace")
# Modules whose public types moved into a companion module (SwiftUI's View lives in SwiftUICore).
COMPANIONS = {"SwiftUI": ("SwiftUICore",), "UIKit": ("UIKitCore",), "Foundation": ("FoundationEssentials",)}
ALWAYS = ("Swift", "Foundation", "_Concurrency")
DECL = re.compile(r"^(?:[@\w() ]*?\s)?(struct|class|enum|protocol|actor|typealias|func|var|let|init|case|extension)"
                  r"\s+(?:[\w.]+\.)?(\w+)")
IMPORT = re.compile(r"^[ \t]*(?:@\w+[ \t]+)*import[ \t]+(?:(?:struct|class|enum|protocol|func|var|let|typealias)"
                    r"[ \t]+)?(\w+)", re.MULTILINE)
ATTRIBUTE_ONLY = re.compile(r"^(?:@\w+(?:\([^()]*(?:\([^()]*\)[^()]*)*\))?\s*)+$")  # e.g. @available(iOS 16, *)
SKIP_DIRS = {".build", "DerivedData", "Pods", "node_modules", ".git"}


@dataclass(frozen=True)
class Source:
    module: str
    path: Path
    public_only: bool  # package sources: only `public`/`open` declarations are API


def sdk_name(root: Path) -> str:
    text = " ".join(p.read_text(errors="replace") for p in (root / "project.yml", root / "Package.swift")
                    if p.is_file())
    if re.search(r"watchOS", text):
        return "watchsimulator"
    if re.search(r"\bmacOS\b|platform:\s*macOS", text) and not re.search(r"\biOS\b", text):
        return "macosx"
    return "iphonesimulator"


def sdk_path(sdk: str) -> Path:
    run = subprocess.run(["xcrun", "--sdk", sdk, "--show-sdk-path"], capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise ApiLookupError(f"no {sdk} SDK: is Xcode installed and selected (xcode-select -p)?")
    return Path(run.stdout.strip())


def sdk_version(sdk: str) -> str:
    run = subprocess.run(["xcrun", "--sdk", sdk, "--show-sdk-version"], capture_output=True, text=True, timeout=60)
    return run.stdout.strip() or "unknown"


def synthesized(module: str, sdk: str, sdk_dir: Path) -> Path | None:
    """The module's full Swift interface (Clang submodules included), generated once per SDK version."""
    version = sdk_version(sdk)
    cache = LawPaths.from_env().cache / "apilookup" / "swift" / f"{sdk}-{version}"
    out = cache / f"{module}.swift"
    if out.is_file() and out.stat().st_size > 0:
        return out
    cache.mkdir(parents=True, exist_ok=True)
    arch = {"iphonesimulator": f"arm64-apple-ios{version}-simulator",
            "watchsimulator": f"arm64-apple-watchos{version}-simulator"}.get(sdk, f"arm64-apple-macos{version}")
    run = subprocess.run(["xcrun", "swift-synthesize-interface", "-include-submodules", "-module-name", module,
                          "-target", arch, "-sdk", str(sdk_dir), "-module-cache-path", str(cache / "module-cache"),
                          "-o", str(out)], capture_output=True, text=True, timeout=600)
    if run.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        return None
    return out


def sdk_interfaces(sdk: Path) -> dict[str, Path]:
    """Module name -> its arm64 .swiftinterface in the SDK (frameworks and the Swift runtime libraries)."""
    found: dict[str, Path] = {}
    patterns = ("System/Library/Frameworks/*.framework/Modules/*.swiftmodule/arm64*.swiftinterface",
                "usr/lib/swift/*.swiftmodule/arm64*.swiftinterface")
    for pattern in patterns:
        for f in sorted(sdk.glob(pattern)):
            if not f.name.endswith(".private.swiftinterface") and not f.name.endswith(".package.swiftinterface"):
                found.setdefault(f.parent.name.removesuffix(".swiftmodule"), f)
    return found


def project_imports(root: Path) -> list[str]:
    mods: dict[str, None] = {}
    for f in swift_files(root):
        for m in IMPORT.findall(f.read_text(errors="replace")):
            mods[m] = None
    return list(mods)


def swift_files(root: Path) -> Iterator[Path]:
    for f in root.rglob("*.swift"):
        if not SKIP_DIRS.intersection(f.relative_to(root).parts):
            yield f


def package_sources(root: Path) -> list[Source]:
    """Swift Package Manager dependency sources: `.build/checkouts` (SwiftPM) and Xcode's SourcePackages."""
    sources = []
    for checkouts in [root / ".build" / "checkouts", *root.glob("**/SourcePackages/checkouts")]:
        for f in sorted(checkouts.glob("*/Sources/**/*.swift")) if checkouts.is_dir() else []:
            sources.append(Source(f.parent.name, f, public_only=True))
    return sources


def block_end(text: str, start: int) -> int:
    """Index just past the brace block that opens at or after `start` (string literals skipped), or the end of
    the line when the declaration has no body."""
    brace = text.find("{", start)
    newline = text.find("\n", start)
    if brace == -1 or (newline != -1 and brace > newline):
        return newline if newline != -1 else len(text)
    depth, i, in_str = 0, brace, False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def brace_delta(line: str) -> int:
    """Opening minus closing braces on a line, ignoring string literals and // comments."""
    delta, in_str, i = 0, False, 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif line.startswith("//", i):
            break
        elif ch == "{":
            delta += 1
        elif ch == "}":
            delta -= 1
        i += 1
    return delta


def signature(line: str) -> str:
    """A member line without its body: `func play() { ... }` -> `func play()`."""
    cut = line.find(" {")
    return (line[:cut] if cut != -1 else line).rstrip("{").rstrip()


def outline(block: str, public_only: bool = False) -> str:
    """A declaration with its body reduced to the direct members' signatures (one line each). For package sources
    (`public_only`) only public and open members are API."""
    lines = block.splitlines()
    if len(lines) <= 2:
        return block.strip()
    out, depth = [signature(lines[0]) + " {"], 1
    for ln in lines[1:-1]:
        stripped = ln.strip()
        visible = not public_only or re.search(r"\b(public|open)\b", stripped)
        if (depth == 1 and stripped and visible and not stripped.startswith(("//", "#"))
                and not ATTRIBUTE_ONLY.match(stripped)):
            out.append("  " + signature(stripped))
        depth += brace_delta(ln)
    out.append("}")
    return "\n".join(out)


def declarations(source: Source, name: str) -> list[str]:
    """Top-level declarations named `name` in one file, and `extension name` blocks."""
    text = source.path.read_text(errors="replace")
    found = []
    for m in re.finditer(rf"^[^\n]*\b(?:struct|class|enum|protocol|actor|typealias|func|extension)\s+"
                         rf"(?:[\w.]+\.)?{re.escape(name)}\b[^\n]*", text, re.MULTILINE):
        line = m.group(0)
        if line.startswith((" ", "\t")):  # nested: not a top-level declaration
            continue
        if source.public_only and not re.search(r"\b(public|open)\b|^extension\b", line):
            continue
        found.append(outline(text[m.start():block_end(text, m.start())], source.public_only))
    return found


def top_level_names(source: Source) -> set[str]:
    names = set()
    for line in source.path.read_text(errors="replace").splitlines():
        if line and not line[0].isspace() and (not source.public_only or re.search(r"\b(public|open)\b", line)):
            m = DECL.match(line)
            if m and m.group(1) != "extension":
                names.add(m.group(2))
    return names


def filter_members(block: str, member: str) -> str | None:
    lines = block.splitlines()
    hits = [ln for ln in lines[1:-1] if member.lower() in ln.lower()]
    return "\n".join([lines[0], *hits, "}"]) if hits else None


def lookup(query: str, member: str | None = None, *, start: Path) -> str:
    root = find_up(start, MARKERS)
    if root is None:
        raise ApiLookupError(f"no Package.swift, project.yml or Xcode project at or above {start}")
    sdk = sdk_name(root)
    sdk_dir = sdk_path(sdk)
    interfaces = sdk_interfaces(sdk_dir)
    module, _, name = query.rpartition(".")
    if module and module not in interfaces:
        module, name = "", query  # a nested name such as Text.Scale is searched whole below
    if module:
        first = [module, *COMPANIONS.get(module, ())]
    else:
        imported = project_imports(root)
        first = list(dict.fromkeys(m for i in [*imported, *ALWAYS] for m in (i, *COMPANIONS.get(i, ()))))
    primary = []
    for m in first:
        full = synthesized(m, sdk, sdk_dir)
        if full is not None:
            primary.append(Source(m, full, False))
        elif m in interfaces:
            primary.append(Source(m, interfaces[m], False))
    rounds = [primary, package_sources(root)]
    if not module:
        rounds.append([Source(m, p, False) for m, p in interfaces.items() if m not in first])
    simple = name.rsplit(".", 1)[-1]
    for sources in rounds:
        blocks = [(s.module, b) for s in sources for b in declarations(s, simple)]
        if member:
            blocks = [(m, f) for m, b in blocks if (f := filter_members(b, member))]
        if blocks:
            return clip("\n\n".join(f"// {m} ({sdk})\n{b}" for m, b in blocks[:8]))
    pool = {n for sources in rounds[:2] for s in sources for n in top_level_names(s)}
    where = f"module {module}" if module else f"the modules {root.name} imports ({sdk} SDK) or its packages"
    if member:
        return not_found("member", f"{name}.{member}", where, [])
    return not_found("declaration", name, where, close_names(simple, pool))
