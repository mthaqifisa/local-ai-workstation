"""`law api` for Go, TypeScript and Swift, and the ecosystem detection that picks one."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from law import apilookup
from law.apilookup import golang, swift, typescript
from law.apilookup.common import ApiLookupError, close_names, filter_lines
from law.paths import LawPaths

# ---- detection and shared helpers --------------------------------------------------------------------------------


def test_the_innermost_project_marker_picks_the_language(tmp_path: Path):
    (tmp_path / "pom.xml").write_text("<project/>")
    web = tmp_path / "frontend"
    web.mkdir()
    (web / "package.json").write_text("{}")
    ios = tmp_path / "ios" / "App.xcodeproj"
    ios.mkdir(parents=True)
    assert apilookup.detect(tmp_path) == "java"
    (web / "src").mkdir()
    assert apilookup.detect(web / "src") == "ts"
    assert apilookup.detect(tmp_path / "ios") == "swift"
    with pytest.raises(ApiLookupError, match="unknown language"):
        apilookup.lookup("X", start=tmp_path, lang="cobol")


def test_close_names_and_member_filter():
    assert close_names("Clinet", ["Client", "ClientConn", "Server"])[:2] == ["Client", "ClientConn"]
    assert filter_lines("class A {\n  get()\n  post()\n}", "POST") == "class A {\n  post()"


# ---- Go ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("query,expected", [
    ("net/http.Client.Do", ("net/http", "Client.Do")),
    ("http.Client", ("http", "Client")),
    ("github.com/gorilla/websocket.Upgrader", ("github.com/gorilla/websocket", "Upgrader")),
    ("github.com/gorilla/websocket", ("github.com/gorilla/websocket", None)),
    ("strings", ("strings", None)),
])
def test_go_query_split(query: str, expected: tuple[str, str | None]):
    assert golang.split_query(query) == expected


@pytest.mark.skipif(shutil.which("go") is None, reason="needs Go")
def test_go_lookup_answers_from_go_doc(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/t\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    assert "func (c *Client) Do(req *Request) (*Response, error)" in golang.lookup("http.Client", "Do",
                                                                                   start=tmp_path)
    miss = golang.lookup("http.Clinet", start=tmp_path)
    assert miss.startswith("No symbol named 'Clinet'") and "Client" in miss
    assert "Client.Do" in golang.lookup("net/http.Client.Doo", start=tmp_path)
    assert "go get" in golang.lookup("example.com/nope.Thing", start=tmp_path)


# ---- TypeScript ----------------------------------------------------------------------------------------------------

LAW_TS = LawPaths.from_env().runtime / "api-tools" / "node_modules" / "typescript"


def test_typescript_7_is_not_used_for_its_missing_compiler_api(tmp_path: Path):
    native = tmp_path / "node_modules" / "typescript"
    (native / "lib").mkdir(parents=True)
    (native / "package.json").write_text(json.dumps({"version": "7.0.2"}))
    assert not typescript.has_compiler_api(native)
    with pytest.raises(ApiLookupError, match="api-tools"):
        typescript.typescript_lib(tmp_path, own=tmp_path / "missing")


@pytest.mark.skipif(shutil.which("node") is None or not typescript.has_compiler_api(LAW_TS),
                    reason="needs node and the api-tools TypeScript")
def test_typescript_lookup_reads_declarations_through_package_exports(tmp_path: Path, monkeypatch):
    real = typescript.typescript_lib
    monkeypatch.setattr(typescript, "typescript_lib", lambda root, own=None: real(root, LAW_TS))  # sandboxed LAW_HOME
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"widgets": "1.0.0"}}))
    pkg = tmp_path / "node_modules" / "widgets"
    (pkg / "types").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({
        "name": "widgets", "version": "1.0.0",
        "exports": {".": {"types": "./types/index.d.ts"}, "./http": {"types": "./types/http.d.ts"}}}))
    (pkg / "types" / "index.d.ts").write_text("export { Client } from './client';\n"
                                             "export declare function useWidget(id: number): string;\n")
    (pkg / "types" / "client.d.ts").write_text("export declare class Client {\n  get(url: string): Promise<string>;\n"
                                              "  post(url: string, body: unknown): Promise<void>;\n}\n")
    (pkg / "types" / "http.d.ts").write_text("export declare const TIMEOUT_MS: number;\n")
    out = typescript.lookup("widgets:Client", "post", start=tmp_path)
    assert "post(url: string, body: unknown): Promise<void>;" in out and "get(" not in out
    assert "declare function useWidget(id: number): string;" in typescript.lookup("useWidget", start=tmp_path)
    assert "TIMEOUT_MS: number" in typescript.lookup("TIMEOUT_MS", start=tmp_path)  # found via the ./http entry
    miss = typescript.lookup("widgets:Clinet", start=tmp_path)
    assert miss.startswith("No export named 'Clinet'") and "Client" in miss
    assert "npm install nope" in typescript.lookup("nope:X", start=tmp_path)


# ---- Swift ---------------------------------------------------------------------------------------------------------


def test_swift_imports_are_read_line_by_line(tmp_path: Path):
    (tmp_path / "App").mkdir()
    (tmp_path / "App" / "A.swift").write_text("import SwiftUI\n\nstruct A: View {}\n@testable import Core\n"
                                              "import struct Foundation.URL\n")
    (tmp_path / ".build").mkdir()
    (tmp_path / ".build" / "Dep.swift").write_text("import Ignored\n")
    assert swift.project_imports(tmp_path) == ["SwiftUI", "Core", "Foundation"]


def test_swift_declarations_outline_and_public_filter(tmp_path: Path):
    src = tmp_path / "Lib.swift"
    src.write_text('@available(iOS 16.0, *)\npublic struct Player {\n  @available(iOS 17.0, *)\n'
                   '  public func play(at time: Double) { let s = "{"; _ = s }\n  public var volume: Float\n'
                   '  struct Hidden {\n    func inner() {}\n  }\n}\n\nstruct Internal {}\n\n'
                   'extension Player {\n  @MainActor public func stop() {}\n}\n')
    source = swift.Source("Lib", src, public_only=True)
    blocks = swift.declarations(source, "Player")
    assert blocks[0].splitlines() == ["public struct Player {", "  public func play(at time: Double)",
                                      "  public var volume: Float", "}"]  # internal Hidden is not API
    assert "@MainActor public func stop()" in blocks[1]
    assert swift.top_level_names(source) == {"Player"}  # Internal is not public API
    assert swift.filter_members(blocks[0], "VOLUME") == "public struct Player {\n  public var volume: Float\n}"
    assert swift.filter_members(blocks[0], "seek") is None


def test_swift_platform_follows_the_project(tmp_path: Path):
    (tmp_path / "Package.swift").write_text("platforms: [.watchOS(.v10)]")
    assert swift.sdk_name(tmp_path) == "watchsimulator"
    (tmp_path / "Package.swift").write_text("platforms: [.macOS(.v14)]")
    assert swift.sdk_name(tmp_path) == "macosx"
    (tmp_path / "Package.swift").write_text("platforms: [.iOS(.v17), .macOS(.v14)]")
    assert swift.sdk_name(tmp_path) == "iphonesimulator"


# ---- Monkey C ------------------------------------------------------------------------------------------------------


def _ciq_sdk(root: Path) -> Path:
    toybox = root / "doc" / "Toybox"
    (toybox / "WatchUi").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin" / "version.txt").write_text("9.3.0\n")

    def page(title: str, *sigs: str) -> str:
        spans = "".join(f'<li><span class="summary_signature">{s}</span></li>' for s in sigs)
        return f"<html><h1>{title}</h1><ul>{spans}</ul></html>"

    (toybox / "WatchUi.html").write_text(page(
        "Module: Toybox.WatchUi", '<a href="#">pushView</a>(view as <span class="type">WatchUi.Views</span>) as Void',
        "requestUpdate() as Void"))
    (toybox / "WatchUi" / "View.html").write_text(page(
        "Class: Toybox.WatchUi.View", "onUpdate(dc as Graphics.Dc) as Void", "onShow() as Void"))
    return root


def test_monkeyc_lookup_reads_the_sdk_reference(tmp_path: Path, monkeypatch):
    from law.apilookup import monkeyc

    sdk = _ciq_sdk(tmp_path / "sdk")
    cfg = tmp_path / "current-sdk.cfg"
    cfg.write_text(str(sdk))
    assert monkeyc.sdk_root(cfg) == sdk
    monkeypatch.setattr(monkeyc, "CURRENT_SDK", cfg)
    monkeypatch.setattr(monkeyc.sdk_root, "__defaults__", (cfg,))
    project = tmp_path / "watchface"
    project.mkdir()
    (project / "monkey.jungle").write_text("project.manifest = manifest.xml\n")
    assert monkeyc.lookup("View", "update", start=project) == (
        "// Toybox.WatchUi.View (Connect IQ 9.3.0)\nClass: Toybox.WatchUi.View\n"
        "  onUpdate(dc as Graphics.Dc) as Void")
    assert "pushView(view as WatchUi.Views) as Void" in monkeyc.lookup("WatchUi.pushView", start=project)
    miss = monkeyc.lookup("Veiw", start=project)
    assert miss.startswith("No class or module named 'Veiw'") and "WatchUi.View" in miss
