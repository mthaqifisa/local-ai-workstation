"""`law api` for Java: the real public API of a library class on a Maven project's classpath.

Coding agents guess library APIs from memory; when a guess is wrong the compiler says only "cannot find
symbol", and the agent either guesses again or digs through jars for hours (seen in the lan-chat runs with
Spring STOMP). This prints the signatures (`javap -public`) of the class asked for, or, when no such class
exists, the closest real names, so one command replaces the guessing.

Supports Maven projects (pom.xml). The test classpath is resolved once with the dependency plugin and
cached under target/law-api/ until pom.xml changes; JDK classes are looked up by fully qualified name.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from law.apilookup.common import ApiLookupError, clip, close_names, find_up
from law.paths import LawPaths

JDK_API = ("java.", "javax.")  # the JDK's public API packages (indexed with the classpath)
MARKERS = ("pom.xml",)


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def cache(self) -> Path:
        return self.root / "target" / "law-api"


def find_project(start: Path) -> Project:
    """The nearest directory at or above `start` with a pom.xml."""
    root = find_up(start, MARKERS)
    if root is None:
        raise ApiLookupError(f"no pom.xml at or above {start}")
    return Project(root)


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


def managed_artifacts(project: Project, mvn: str = "mvn") -> list[tuple[str, str]]:
    """(groupId, artifactId) of every dependency the effective POM manages (a parent such as Spring Boot's lists
    all its modules), cached until pom.xml changes. Empty when Maven cannot produce it."""
    out = project.cache / "effective-pom.xml"
    pom = project.root / "pom.xml"
    if not out.is_file() or out.stat().st_mtime < pom.stat().st_mtime:
        project.cache.mkdir(parents=True, exist_ok=True)
        try:
            run = subprocess.run([mvn, "-q", "help:effective-pom", f"-Doutput={out}"], cwd=project.root,
                                 capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            return []  # a hint, not the answer: the lookup still reports the class itself
        if run.returncode != 0 or not out.is_file():
            return []
    try:
        root = ET.parse(out).getroot()  # noqa: S314 - written just now by Maven from the project's own POMs
    except ET.ParseError:
        return []
    found: list[tuple[str, str]] = []
    for dep in root.iterfind(".//{*}dependencyManagement/{*}dependencies/{*}dependency"):
        group, artifact = dep.findtext("{*}groupId"), dep.findtext("{*}artifactId")
        if group and artifact:
            found.append((group, artifact))
    return list(dict.fromkeys(found))


def leading_words(segment: str, words: set[str]) -> int:
    """How many of `words` a name segment starts with, read left to right: `resttestclient` -> rest, test = 2;
    `presto` -> 0 (a word inside another word does not count)."""
    count, rest = 0, segment
    while True:
        word = next((w for w in sorted(words, key=len, reverse=True) if rest.startswith(w)), None)
        if word is None:
            return count
        count, rest = count + 1, rest[len(word):]


def unused_modules(query: str, managed: list[tuple[str, str]], entries: list[Path],
                   limit: int = 5) -> list[tuple[str, str]]:
    """Managed artifacts not on the classpath whose names share at least two words with the class name
    (`TestRestTemplate` -> spring-boot-resttestclient). Best match first; groups the project already uses first."""
    words = {w.lower() for w in re.findall(r"[A-Z][a-z]+|[a-z]+", query.rsplit(".", 1)[-1]) if len(w) >= 3}
    present = {e.name for e in entries}
    used_groups = {g for g, a in managed if any(name.startswith(f"{a}-") for name in present)}
    scored = []
    for group, artifact in managed:
        if any(name.startswith(f"{artifact}-") for name in present):
            continue
        score = sum(leading_words(seg, words) for seg in artifact.lower().split("-"))
        if score >= min(2, len(words)):
            scored.append((-score, group not in used_groups, len(artifact), group, artifact))
    return [(g, a) for *_, g, a in sorted(scored)[:limit]]


def jdk_classes(java_home: str | None) -> dict[str, str]:
    """The JDK's public API classes (java.*, javax.*) from its runtime image, so `law api java.net.http.WebSocket`
    and `law api CountDownLatch` work like library classes. Cached per JDK; an empty entry means "no -cp"."""
    home = Path(java_home) if java_home else Path(javap_tool(None)).resolve().parent.parent
    modules = home / "lib" / "modules"
    jimage = home / "bin" / "jimage"
    if not modules.is_file() or not jimage.is_file():
        return {}
    cache = LawPaths.from_env().cache / "apilookup" / "jdk" / f"{re.sub(r'[^\w.-]', '_', str(home))}.json"
    if cache.is_file() and cache.stat().st_mtime >= modules.stat().st_mtime:
        return dict(json.loads(cache.read_text()))
    run = subprocess.run([str(jimage), "list", str(modules)], capture_output=True, text=True, timeout=120)
    found: dict[str, str] = {}
    for line in run.stdout.splitlines():
        fqcn = class_name(line.strip())
        if fqcn and fqcn.startswith(JDK_API):
            found[fqcn] = ""
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(found))
    return found


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
    """Real classes whose simple names are close to a class that does not exist."""
    by_simple: dict[str, list[str]] = {}
    for fqcn in classes:
        by_simple.setdefault(fqcn.rsplit(".", 1)[-1], []).append(fqcn)
    names = close_names(query.rsplit(".", 1)[-1], by_simple, limit)
    return [f for n in names for f in sorted(by_simple[n])[:2]][:limit]


def package_listing(package: str, classes: dict[str, str]) -> list[str]:
    """Top-level classes directly in `package`."""
    return sorted(c for c in classes if c.rsplit(".", 1)[0] == package)


def javap(fqcn: str, entry: str | None, java_home: str | None = None) -> str:
    """`javap -public` of one class; nested classes are passed with `$`."""
    tool = javap_tool(java_home)
    cmd = [tool, "-public", *(["-cp", entry] if entry else []), binary_name(fqcn)]  # no entry: a JDK class
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
    body = hits or [f"  (no public member mentions '{member}', declared or inherited: it does not exist here)"]
    return "\n".join([*header[:-1], *body, *header[-1:]]) if header else "\n".join(body)


def supertypes(javap_text: str) -> list[str]:
    """Fully qualified supertypes named in a javap class header (generics dropped)."""
    header = next((ln for ln in javap_text.splitlines() if not ln.startswith((" ", "Compiled"))), "")
    names: list[str] = []
    for part in re.findall(r"\b(?:extends|implements)\s+(.+?)(?=\s+(?:extends|implements)\b|\s*\{|$)", header):
        depth, current = 0, ""
        for ch in part:
            depth += ch == "<"
            depth -= ch == ">"
            if ch == "," and depth == 0:
                names.append(current)
                current = ""
            elif depth == 0 and ch not in "<>":
                current += ch
        names.append(current)
    return [n.strip() for n in names if n.strip() and not n.strip().startswith("java.lang.Object")]


def inherited(fqcn: str, member: str, classes: dict[str, str], java_home: str | None, depth: int = 4) -> list[str]:
    """Member lines that mention `member` in the supertypes of `fqcn` (javap lists only declared members)."""
    found: list[str] = []
    seen: set[str] = set()
    frontier = [fqcn]
    for _ in range(depth):
        nxt: list[str] = []
        for name in frontier:
            text = javap(name, classes.get(name), java_home)
            for parent in supertypes(text):
                if parent in seen:
                    continue
                seen.add(parent)
                ptext = javap(parent, classes.get(parent), java_home)
                hits = [ln for ln in ptext.splitlines() if ln.startswith(" ") and member.lower() in ln.lower()]
                if hits:
                    found.append(f"  // inherited from {parent}")
                    found.extend(hits)
                nxt.append(parent)
        frontier = nxt
    return found


def lookup(query: str, member: str | None = None, *, start: Path, java_home: str | None = None,
           mvn: str = "mvn") -> str:
    """The answer `law api` prints."""
    query = query.replace("/", ".").removesuffix(".class").removesuffix(".java")
    project = find_project(start)
    classes = {**jdk_classes(java_home), **class_index(project, classpath(project, mvn))}
    matches = resolve(query, classes)
    note = ""
    if not matches and "." in query:
        # A wrong package guess (jakarta.websocket.client.StandardWebSocketClient): the simple name decides.
        matches = resolve(query.rsplit(".", 1)[-1], classes)
        if matches:
            note = f"'{query}' is not a class; the real one is {matches[0]}:\n"
    if not matches:
        listing = package_listing(query, classes)
        if listing:
            return clip(f"Package {query} ({len(listing)} classes):\n" + "\n".join(listing))
        near = suggestions(query, classes)
        hint = ("\nClosest real classes on the classpath:\n" + "\n".join(f"  {n}" for n in near)) if near else ""
        modules = unused_modules(query, managed_artifacts(project, mvn), classpath(project, mvn))
        add = ("\nIt may live in a module this project has not added. Managed modules whose names match "
               "(add one as a dependency, version from the parent, then run law api again):\n"
               + "\n".join(f"  <dependency><groupId>{g}</groupId><artifactId>{a}</artifactId></dependency>"
                            for g, a in modules)) if modules else ""
        return (f"No class named '{query}' on the test classpath of {project.root.name}: it is not in the project's "
                f"current dependencies.{hint}{add}\nIf neither fits, it does not exist in these versions: do not use "
                "it.")
    out: list[str] = [note.rstrip("\n")] if note else []
    if len(matches) > 1:
        out.append(f"{len(matches)} classes named '{query}': " + ", ".join(matches[:10]))
    for fqcn in matches[:3]:
        text = javap(fqcn, classes[fqcn], java_home)
        if member and not any(ln.startswith(" ") and member.lower() in ln.lower() for ln in text.splitlines()):
            extra = inherited(fqcn, member, classes, java_home)
            if extra:
                header = [ln for ln in text.splitlines() if not ln.startswith(" ")]
                out.append("\n".join([*header[:-1], *extra, *header[-1:]]))
                continue
        out.append(filter_members(text, member) if member else text)
    return clip("\n\n".join(out))

