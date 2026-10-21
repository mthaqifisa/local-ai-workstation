"""`law api`: the real public API of a library class on a project's classpath, in one command.

Coding agents guess library APIs from memory; when a guess is wrong the compiler says only "cannot find
symbol", and the agent either guesses again or digs through jars for hours (seen in the lan-chat runs with
Spring STOMP). This prints the signatures (`javap -public`) of the class asked for, or, when no such class
exists, the closest real names, so one command replaces the guessing.

Supports Maven projects (pom.xml). The test classpath is resolved once with the dependency plugin and
cached under target/law-api/ until pom.xml changes; JDK classes are looked up by fully qualified name.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_LINES = 200
JDK_PREFIXES = ("java.", "javax.", "jdk.", "sun.")


class ApiLookupError(Exception):
    """A lookup that cannot run (no supported project, classpath not resolvable, no javap)."""


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def cache(self) -> Path:
        return self.root / "target" / "law-api"


def find_project(start: Path) -> Project:
    """The nearest directory at or above `start` with a pom.xml."""
    for d in (start.resolve(), *start.resolve().parents):
        if (d / "pom.xml").is_file():
            return Project(d)
    raise ApiLookupError(f"no pom.xml at or above {start}; `law api` supports Maven projects")


def classpath(project: Project, mvn: str = "mvn") -> list[Path]:
    """Jars and class directories of the test classpath, cached until pom.xml changes."""
    project.cache.mkdir(parents=True, exist_ok=True)
    out = project.cache / "classpath.txt"
    pom = project.root / "pom.xml"
    if not out.is_file() or out.stat().st_mtime < pom.stat().st_mtime:
        run = subprocess.run([mvn, "-q", "dependency:build-classpath", f"-Dmdep.outputFile={out}",
                              "-Dmdep.includeScope=test"], cwd=project.root, capture_output=True, text=True,
                             timeout=600)
        if run.returncode != 0 or not out.is_file():
            tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-15:])
            raise ApiLookupError(f"could not resolve the Maven classpath:\n{tail}")
    entries = [Path(p) for p in out.read_text().strip().split(":") if p]
    own = [project.root / "target" / "classes", project.root / "target" / "test-classes"]
    return [p for p in (*own, *entries) if p.exists()]


def class_index(project: Project, entries: list[Path]) -> dict[str, str]:
    """Fully qualified class name (nested classes as Outer.Inner) -> the classpath entry that holds it."""
    key = [f"{p}:{p.stat().st_mtime}" for p in entries]
    cached = project.cache / "index.json"
    if cached.is_file():
        data = json.loads(cached.read_text())
        if data.get("key") == key:
            return dict(data["classes"])
    classes: dict[str, str] = {}
    for entry in entries:
        names = (zipfile.ZipFile(entry).namelist() if entry.is_file()
                 else [str(f.relative_to(entry)) for f in entry.rglob("*.class")])
        for name in names:
            fqcn = class_name(name)
            if fqcn:
                classes.setdefault(fqcn, str(entry))
    cached.write_text(json.dumps({"key": key, "classes": classes}))
    return classes


def class_name(entry: str) -> str | None:
    """`org/x/A$B.class` -> `org.x.A.B`; None for resources, module/package info and anonymous classes."""
    if not entry.endswith(".class") or entry.startswith("META-INF/"):
        return None
    stem = entry[: -len(".class")]
    if stem.endswith(("module-info", "package-info")) or re.search(r"\$\d", stem):
        return None
    return stem.replace("/", ".").replace("$", ".")


def resolve(query: str, classes: dict[str, str]) -> list[str]:
    """Classes matching `query`: a fully qualified name, a simple name, or a nested name (Outer.Inner)."""
    if query in classes:
        return [query]
    suffix = "." + query
    return sorted(c for c in classes if c.endswith(suffix))


def suggestions(query: str, classes: dict[str, str], limit: int = 8) -> list[str]:
    """Real class names close to a name that does not exist (similar spelling first, then a shared prefix)."""
    simple = query.rsplit(".", 1)[-1]
    by_simple: dict[str, list[str]] = {}
    for fqcn in classes:
        by_simple.setdefault(fqcn.rsplit(".", 1)[-1], []).append(fqcn)
    close = difflib.get_close_matches(simple, list(by_simple), n=limit, cutoff=0.6)
    prefix = sorted(n for n in by_simple if len(simple) >= 4 and n.startswith(simple[:4]))
    names = list(dict.fromkeys([*close, *prefix]))
    return [f for n in names for f in sorted(by_simple[n])[:2]][:limit]


def package_listing(package: str, classes: dict[str, str]) -> list[str]:
    """Top-level classes directly in `package`."""
    return sorted(c for c in classes if c.rsplit(".", 1)[0] == package)


def javap(fqcn: str, entry: str | None, java_home: str | None = None) -> str:
    """`javap -public` of one class; nested classes are passed with `$`."""
    tool = javap_tool(java_home)
    cmd = [tool, "-public", *(["-cp", entry] if entry else []), binary_name(fqcn)]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return (run.stdout or run.stderr).strip()


def javap_tool(java_home: str | None) -> str:
    """The project JDK's javap (JAVA_HOME), else the first one on PATH."""
    home = Path(java_home) / "bin" / "javap" if java_home else None
    tool = str(home) if home and home.is_file() else shutil.which("javap")
    if tool is None:
        raise ApiLookupError("javap not found: source the project's .law/env.sh (it sets JAVA_HOME)")
    return tool


def binary_name(fqcn: str) -> str:
    """`a.b.Outer.Inner` -> `a.b.Outer$Inner`: the first capitalised segment starts the class name."""
    parts = fqcn.split(".")
    first = next((i for i, p in enumerate(parts) if p[:1].isupper()), len(parts) - 1)
    return ".".join(parts[: first + 1]) + "".join("$" + p for p in parts[first + 1:])


def filter_members(text: str, member: str) -> str:
    """The class header plus the member lines that mention `member` (case-insensitive)."""
    lines = text.splitlines()
    header = [ln for ln in lines if not ln.startswith(" ")]
    hits = [ln for ln in lines if ln.startswith(" ") and member.lower() in ln.lower()]
    body = hits or [f"  (no public member mentions '{member}')"]
    return "\n".join([*header[:-1], *body, *header[-1:]]) if header else "\n".join(body)


def lookup(query: str, member: str | None = None, *, start: Path, java_home: str | None = None,
           mvn: str = "mvn") -> str:
    """The answer `law api` prints."""
    if query.startswith(JDK_PREFIXES):
        text = javap(query, None, java_home)
        return clip(filter_members(text, member) if member else text)
    project = find_project(start)
    classes = class_index(project, classpath(project, mvn))
    matches = resolve(query, classes)
    if not matches:
        listing = package_listing(query, classes)
        if listing:
            return clip(f"Package {query} ({len(listing)} classes):\n" + "\n".join(listing))
        near = suggestions(query, classes)
        hint = ("\nClosest real classes:\n" + "\n".join(f"  {n}" for n in near)) if near else ""
        return (f"No class named '{query}' on the test classpath of {project.root.name}. "
                f"It does not exist in these library versions: do not use it.{hint}")
    out: list[str] = []
    if len(matches) > 1:
        out.append(f"{len(matches)} classes named '{query}': " + ", ".join(matches[:10]))
    for fqcn in matches[:3]:
        text = javap(fqcn, classes[fqcn], java_home)
        out.append(filter_members(text, member) if member else text)
    return clip("\n\n".join(out))


def clip(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= MAX_LINES:
        return text
    return "\n".join([*lines[:MAX_LINES], f"... {len(lines) - MAX_LINES} more lines: add a member filter"])
