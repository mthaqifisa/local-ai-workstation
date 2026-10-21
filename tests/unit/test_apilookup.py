"""`law api`: class index, name resolution, suggestions for names that do not exist, and javap output."""

from __future__ import annotations

import os
import shutil
import time
import zipfile
from pathlib import Path

import pytest

from law.apilookup import java as apilookup

STOMP = "org/springframework/messaging/simp/stomp/"


@pytest.fixture(autouse=True)
def small_jdk(monkeypatch, request):
    """Most tests use a tiny JDK index; test_jdk_* tests read the real runtime image."""
    if not request.node.name.startswith("test_jdk"):
        monkeypatch.setattr(apilookup, "jdk_classes", lambda java_home: {"java.util.concurrent.CountDownLatch": ""})


def _project(tmp_path: Path) -> apilookup.Project:
    """A Maven project whose cached classpath is one fake jar (no mvn run needed)."""
    (tmp_path / "pom.xml").write_text("<project/>")
    jar = tmp_path / "lib" / "spring-messaging.jar"
    jar.parent.mkdir()
    with zipfile.ZipFile(jar, "w") as z:
        for name in ("StompSession", "StompSession$Subscription", "StompSession$Receiptable", "StompHeaders",
                     "StompFrameHandler", "StompSessionHandlerAdapter", "StompCommand", "StompSession$1"):
            z.writestr(f"{STOMP}{name}.class", b"")
        z.writestr("META-INF/MANIFEST.MF", "")
        z.writestr(f"{STOMP}package-info.class", b"")
    project = apilookup.Project(tmp_path)
    project.cache.mkdir(parents=True)
    cp = project.cache / "classpath.txt"
    cp.write_text(str(jar))
    future = time.time() + 5
    os.utime(cp, (future, future))
    return project


def test_class_name_skips_resources_and_anonymous_classes():
    assert apilookup.class_name("a/b/Outer$Inner.class") == "a.b.Outer.Inner"
    assert apilookup.class_name("a/b/Outer$1.class") is None
    assert apilookup.class_name("a/b/package-info.class") is None
    assert apilookup.class_name("META-INF/versions/21/a/B.class") is None
    assert apilookup.class_name("a/b/c.properties") is None


def test_index_resolve_and_package_listing(tmp_path: Path):
    project = _project(tmp_path)
    classes = apilookup.class_index(project, apilookup.classpath(project, mvn="/nonexistent/mvn"))
    pkg = "org.springframework.messaging.simp.stomp"
    assert apilookup.resolve("StompSession", classes) == [f"{pkg}.StompSession"]
    assert apilookup.resolve("StompSession.Subscription", classes) == [f"{pkg}.StompSession.Subscription"]
    assert apilookup.resolve(f"{pkg}.StompHeaders", classes) == [f"{pkg}.StompHeaders"]
    assert f"{pkg}.StompSession.1" not in classes
    assert apilookup.package_listing(pkg, classes)[0] == f"{pkg}.StompCommand"
    assert (project.cache / "index.json").is_file()


def test_unknown_class_names_the_closest_real_ones(tmp_path: Path):
    """The lan-chat failure: the agent invented `StompFrame`; the answer must point at what exists."""
    _project(tmp_path)
    out = apilookup.lookup("StompFrame", start=tmp_path, mvn="/nonexistent/mvn")
    assert out.startswith("No class named 'StompFrame'") and "do not use it" in out
    assert "org.springframework.messaging.simp.stomp.StompFrameHandler" in out


def test_find_project_walks_up_and_fails_clearly(tmp_path: Path):
    (tmp_path / "pom.xml").write_text("<project/>")
    sub = tmp_path / "src" / "main"
    sub.mkdir(parents=True)
    assert apilookup.find_project(sub).root == tmp_path.resolve()
    with pytest.raises(apilookup.ApiLookupError, match=r"no pom\.xml"):
        apilookup.find_project(Path("/"))


def test_classpath_failure_shows_maven_output(tmp_path: Path):
    (tmp_path / "pom.xml").write_text("<project/>")
    fake = tmp_path / "mvn"
    fake.write_text("#!/bin/sh\necho '[ERROR] Non-resolvable parent POM'\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(apilookup.ApiLookupError, match="Non-resolvable parent POM"):
        apilookup.classpath(apilookup.Project(tmp_path), mvn=str(fake))


def test_binary_name_and_member_filter():
    assert apilookup.binary_name("a.b.Outer.Inner") == "a.b.Outer$Inner"
    text = ("public interface a.B {\n  public abstract void subscribe(java.lang.String);\n"
            "  public abstract void send(java.lang.String);\n}")
    filtered = apilookup.filter_members(text, "SUBSCRIBE")
    assert "subscribe" in filtered and "send" not in filtered and filtered.endswith("}")
    assert "no public member mentions 'x', declared or inherited" in apilookup.filter_members(text, "x")


@pytest.mark.skipif(shutil.which("jimage") is None and not os.environ.get("JAVA_HOME"), reason="needs a JDK")
def test_jdk_classes_are_indexed_like_library_classes(tmp_path: Path):
    _project(tmp_path)
    home = os.environ.get("JAVA_HOME")
    by_path = apilookup.lookup("java/net/http/WebSocket", start=tmp_path, java_home=home, mvn="/nonexistent/mvn")
    assert "public interface java.net.http.WebSocket" in by_path
    simple = apilookup.lookup("CountDownLatch", "await", start=tmp_path, java_home=home, mvn="/nonexistent/mvn")
    assert "await(long, java.util.concurrent.TimeUnit)" in simple and "countDown()" not in simple


def test_javap_falls_back_to_path_when_java_home_has_none(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "javap"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert apilookup.javap_tool(str(tmp_path / "jdk-without-javap")) == str(fake)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(apilookup.ApiLookupError, match="javap not found"):
        apilookup.javap_tool(None)


def test_leading_words_count_only_at_segment_starts():
    words = {"test", "rest", "template"}
    assert apilookup.leading_words("resttestclient", words) == 2
    assert apilookup.leading_words("presto", words) == 0
    assert apilookup.leading_words("templates", words) == 1


def test_a_missing_class_points_at_the_managed_module_that_holds_it(tmp_path: Path):
    """Spring Boot 4 moved TestRestTemplate into spring-boot-resttestclient; the answer must name that module."""
    project = _project(tmp_path)
    (project.cache / "effective-pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencyManagement><dependencies>'
        + "".join(f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>1</version></dependency>"
                  for g, a in [("org.springframework.boot", "spring-boot-resttestclient"),
                               ("org.springframework.boot", "spring-messaging"),
                               ("org.testcontainers", "testcontainers-presto"),
                               ("org.springframework.boot", "spring-boot-test")])
        + "</dependencies></dependencyManagement></project>")
    future = time.time() + 10
    os.utime(project.cache / "effective-pom.xml", (future, future))
    out = apilookup.lookup("TestRestTemplate", start=tmp_path, mvn="/nonexistent/mvn")
    assert "may live in a module this project has not added" in out
    assert "<artifactId>spring-boot-resttestclient</artifactId>" in out
    assert "presto" not in out and "spring-messaging" not in out


def test_a_wrong_package_guess_is_answered_with_the_real_class(tmp_path: Path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setattr(apilookup, "javap", lambda fqcn, entry, java_home=None: f"interface {fqcn} {{}}")
    out = apilookup.lookup("jakarta/websocket/StompSession", start=tmp_path, mvn="/nonexistent/mvn")
    assert out.splitlines()[0] == ("'jakarta.websocket.StompSession' is not a class; the real one is "
                                   "org.springframework.messaging.simp.stomp.StompSession:")
    assert "interface org.springframework.messaging.simp.stomp.StompSession" in out


def test_inherited_members_are_found_in_supertypes(tmp_path: Path, monkeypatch):
    _project(tmp_path)
    pkg = "org.springframework.messaging.simp.stomp."
    pages = {
        pkg + "StompSessionHandlerAdapter": f"public abstract class {pkg}StompSessionHandlerAdapter implements "
                                            f"{pkg}StompSessionHandler {{\n  public {pkg}StompSessionHandlerAdapter();\n}}",
        pkg + "StompSessionHandler": f"public interface {pkg}StompSessionHandler extends {pkg}StompFrameHandler {{\n"
                                     f"  public abstract void afterConnected({pkg}StompSession, {pkg}StompHeaders);\n}}",
        pkg + "StompFrameHandler": f"public interface {pkg}StompFrameHandler {{\n  public abstract void handleFrame();\n}}",
    }
    monkeypatch.setattr(apilookup, "javap", lambda fqcn, entry, java_home=None: pages.get(fqcn, f"class {fqcn} {{\n}}"))
    assert apilookup.supertypes(pages[pkg + "StompSessionHandler"]) == [pkg + "StompFrameHandler"]
    out = apilookup.lookup("StompSessionHandlerAdapter", "handleFrame", start=tmp_path, mvn="/nonexistent/mvn")
    assert f"  // inherited from {pkg}StompFrameHandler" in out and "public abstract void handleFrame();" in out
    assert "declared or inherited" in apilookup.lookup("StompSessionHandlerAdapter", "nope", start=tmp_path,
                                                       mvn="/nonexistent/mvn")
