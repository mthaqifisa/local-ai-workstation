"""The read-only Console (ADR-0022): nothing can change LAW and only allowlisted fields leave."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from law import public_console
from law.paths import LawPaths

REPO = Path(__file__).resolve().parents[2]
PRIVATE = "my private words"
BACKEND: dict[str, Any] = {
    "/system": {"cpu_percent": 5, "hostname": PRIVATE},
    "/healthz": {"ok": True, "queued": 1, "secret": PRIVATE},
    "/agents": {"model": {"active": True, "loaded": ["m"], "log": PRIVATE}, "jobs": [{
        "id": "a1", "kind": "task", "title": PRIVATE, "status": "running", "health": "working", "error": PRIVATE,
        "error_line": PRIVATE, "recent": [{"message": PRIVATE}], "last_action": {"message": PRIVATE},
        "last_10min": {"reads": 1}, "actions": ["cancel"], "project": "/home/x/Pantry",
        "stage": {"index": 1, "total": 3, "kind": "task_work", "prompt": PRIVATE},
        "plan": {"done": 1, "total": 2, "next": "P-2 Home", "progress": PRIVATE}}]},
    "/jobs?limit=120": [{"id": "a1", "kind": "ask", "status": "done", "error": PRIVATE, "reply_to": {"chat_id": 1},
                         "payload": {"prompt": PRIVATE, "history": [PRIVATE]}}],
    "/board": [{"id": "pantry", "name": "Pantry", "state": "done", "version": 1, "dir": PRIVATE}],
    "/board/pantry": {
        "project": {"id": "pantry", "name": "Pantry", "state": "awaiting_go", "version": 1, "dir": PRIVATE,
                    "summary": PRIVATE, "approve": "go", "live": False, "history": [{"note": PRIVATE}]},
        "columns": [{"id": "todo", "label": "To do"}], "notes": [{"text": PRIVATE}],
        "tickets": [{"id": "P-1", "title": "Home screen", "kind": "dev", "column": "todo", "details": PRIVATE,
                     "logs": [{"text": PRIVATE}], "notes": [{"text": PRIVATE}], "jobs": [PRIVATE],
                     "steps": [{"stage": 1, "kind": "task_work", "job": PRIVATE}]}]},
}


@pytest.fixture()
def public(sandbox: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "settings", repo / "settings")
    paths = LawPaths(repo=repo, home=sandbox / "LAW", user_home=sandbox)
    paths.env_file.parent.mkdir(parents=True, exist_ok=True)
    paths.env_file.write_text("LAW_TELEGRAM_TOKEN=super-secret-value\n")
    asked: list[str] = []

    async def fetch(path: str) -> Any:
        asked.append(path)
        return BACKEND[path]

    with TestClient(public_console.create_public_app(paths, fetch)) as client:
        yield client, asked


@pytest.mark.parametrize("path", ["/healthz", "/system", "/agents", "/jobs", "/board", "/board/pantry"])
def test_private_text_never_leaves(public, path):
    client, _ = public
    r = client.get(path)
    assert r.status_code == 200 and PRIVATE not in r.text


def test_the_page_can_still_draw_what_it_gets(public):
    client, _ = public
    board = client.get("/board/pantry").json()
    assert board["tickets"][0]["title"] == "Home screen" and board["project"]["approve"] is None
    assert board["tickets"][0]["logs"] == [] and board["tickets"][0]["notes"] == []
    job = client.get("/agents").json()["jobs"][0]
    assert job["title"] == "Pantry" and job["actions"] == [] and job["plan"]["next"] == "P-2 Home"


@pytest.mark.parametrize("method,path", [
    ("post", "/jobs"), ("post", "/jobs/a1/cancel"), ("post", "/jobs/a1/retry"), ("post", "/board/pantry/approve"),
    ("post", "/board/pantry/notes"), ("delete", "/board/pantry"), ("post", "/reset"), ("post", "/storage/clear"),
    ("put", "/roles/coder"), ("put", "/settings/telegram"), ("post", "/send"), ("post", "/agents"), ("put", "/board"),
    ("delete", "/jobs"), ("get", "/settings"), ("get", "/roles"), ("get", "/storage"), ("get", "/files"),
    ("get", "/client/install.sh"), ("get", "/jobs/a1"), ("get", "/jobs/a1/events"), ("get", "/docs"),
    ("get", "/openapi.json"), ("get", "/redoc")])
def test_only_the_read_routes_exist(public, method, path):
    client, asked = public
    assert getattr(client, method)(path).status_code in {404, 405}
    assert asked == []


@pytest.mark.parametrize("pid", ["..%2f..%2fsettings", "a b", "x" * 80, "a%0ab"])
def test_a_strange_project_id_never_reaches_the_api(public, pid):
    client, asked = public
    assert client.get(f"/board/{pid}").status_code == 404 and asked == []


def test_only_get_routes_are_registered(sandbox, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "settings", repo / "settings")
    app = public_console.create_public_app(LawPaths(repo=repo, home=sandbox / "LAW", user_home=sandbox))
    assert {m for r in app.routes for m in getattr(r, "methods", set())} <= {"GET", "HEAD"}


def test_a_flood_costs_one_api_call(public):
    client, asked = public
    for _ in range(50):
        client.get("/system")
    assert asked == ["/system"]


def test_the_page_is_read_only_and_headers_are_strict(public):
    client, _ = public
    r = client.get("/")
    assert "<html data-readonly" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


def test_redact_is_the_net_under_the_allowlists():
    text = "token 123456789:" + "a" * 35 + " sk-" + "b" * 24 + " me@example.com /home/x/a my-secret-value"
    out = json.dumps(public_console.redact({"a": [text]}, {"my-secret-value"}, "/home/x"))
    assert "a" * 35 not in out and "b" * 24 not in out and "example.com" not in out
    assert "/home/x" not in out and "my-secret-value" not in out
