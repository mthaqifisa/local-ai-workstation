"""`law api` for Monkey C (Garmin Connect IQ watch apps, watch faces, data fields, widgets): signatures from the
installed Connect IQ SDK's API reference (its doc/Toybox pages), so the answer matches the SDK the project builds with.

Queries: `WatchUi.View` (a class: its methods, constants and typedefs), `View` (searched in every Toybox module),
`WatchUi` (a module: its functions, classes and constants), `WatchUi pushView` (a member filter).
The SDK is the one in ~/Library/Application Support/Garmin/ConnectIQ/current-sdk.cfg, else the `monkeyc` on PATH.
"""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

from law.apilookup.common import ApiLookupError, clip, close_names, filter_lines, find_up, not_found

MARKERS = ("monkey.jungle", "manifest.xml")
CURRENT_SDK = Path.home() / "Library" / "Application Support" / "Garmin" / "ConnectIQ" / "current-sdk.cfg"
SIGNATURE = re.compile(r'<span class="summary_signature[^"]*">(.*?)</span>\s*(?=<|$)', re.S)
TITLE = re.compile(r"<h1>(.*?)</h1>", re.S)


def sdk_root(current: Path = CURRENT_SDK) -> Path:
    if current.is_file():
        path = Path(current.read_text().strip())
        if (path / "doc" / "Toybox").is_dir():
            return path
    monkeyc = shutil.which("monkeyc")
    if monkeyc:
        root = Path(monkeyc).resolve().parent.parent
        if (root / "doc" / "Toybox").is_dir():
            return root
    raise ApiLookupError("no Connect IQ SDK: run `law install` (brew cask connectiq)")


def text_of(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def pages(sdk: Path) -> dict[str, Path]:
    """`WatchUi.View` -> its page, `WatchUi` -> the module page (nested modules keep their path: `Lang.Number`)."""
    toybox = sdk / "doc" / "Toybox"
    return {".".join(p.relative_to(toybox).with_suffix("").parts): p for p in toybox.rglob("*.html")}


def resolve(query: str, index: dict[str, Path]) -> list[str]:
    q = query.removeprefix("Toybox.")
    if q in index:
        return [q]
    return sorted(k for k in index if k.endswith("." + q))


def page_text(path: Path) -> str:
    """A page as declarations: its title (e.g. `Class: Toybox.WatchUi.View`) and every summary signature."""
    raw = path.read_text(errors="replace")
    title = TITLE.search(raw)
    head = text_of(title.group(1)) if title else path.stem
    sigs = list(dict.fromkeys(text_of(m) for m in SIGNATURE.findall(raw)))
    return "\n".join([head, *(f"  {s}" for s in sigs if s)])


def lookup(query: str, member: str | None = None, *, start: Path) -> str:
    if find_up(start, MARKERS) is None:
        raise ApiLookupError(f"no monkey.jungle or manifest.xml at or above {start}")
    sdk = sdk_root()
    index = pages(sdk)
    matches = resolve(query, index)
    version = (sdk / "bin" / "version.txt").read_text().strip() if (sdk / "bin" / "version.txt").is_file() else "?"
    if not matches:
        # A module function or constant asked as Module.name: show the module page filtered to it.
        module, _, name = query.removeprefix("Toybox.").rpartition(".")
        if module in index and not member:
            text = filter_lines(page_text(index[module]), name)
            if "(no member mentions" not in text:
                return clip(f"// Toybox.{module} (Connect IQ {version})\n{text}")
        near = close_names(query.rsplit(".", 1)[-1], [k.rsplit(".", 1)[-1] for k in index])
        full = [k for n in near for k in sorted(index) if k.rsplit(".", 1)[-1] == n][:8]
        return not_found("class or module", query, f"Connect IQ SDK {version}", full)
    blocks = []
    for name in matches[:3]:
        text = page_text(index[name])
        blocks.append(f"// Toybox.{name} (Connect IQ {version})\n{filter_lines(text, member) if member else text}")
    return clip("\n\n".join(blocks))
