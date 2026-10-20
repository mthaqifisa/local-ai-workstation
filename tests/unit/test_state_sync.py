"""law save / restore-state against a fake Open WebUI: clean export, secret guard, add-only restore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from law import state_sync as s


class FakeWebUI:
    def __init__(self) -> None:
        self.prompts: list[dict[str, Any]] = [{"id": "u1", "command": "memo", "name": "Memo", "content": "Write",
                                               "user_id": "me", "updated_at": 1}]
        self.knowledge: list[dict[str, Any]] = [{"id": "k1", "name": "Policies", "description": "HR", "files": []}]
        self.tools: list[dict[str, Any]] = [{"id": "fx", "name": "FX", "content": "code", "meta": {},
                                             "valves": {"api_key": "x"}}]
        self.created: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        if path == "/api/v1/auths/signin":
            return httpx.Response(200, json={"token": "t"})
        if method == "GET":
            data = {"/api/v1/models/export": [], "/api/v1/prompts/": self.prompts, "/api/v1/tools/export": self.tools,
                    "/api/v1/tools/": self.tools, "/api/v1/functions/export": [], "/api/v1/functions/": [],
                    "/api/v1/knowledge/": {"items": self.knowledge, "total": len(self.knowledge)}}[path]
            return httpx.Response(200, json=data)
        body = json.loads(req.content)
        self.created.append(path)
        target = {"/api/v1/prompts/create": self.prompts, "/api/v1/knowledge/create": self.knowledge,
                  "/api/v1/tools/create": self.tools}.get(path)
        if target is not None:
            target.append(body)
        return httpx.Response(200, json=True)


@pytest.fixture
def fake() -> FakeWebUI:
    return FakeWebUI()


def _api(fake: FakeWebUI) -> s.OpenWebUI:
    return s.OpenWebUI("http://owui", transport=httpx.MockTransport(fake.handler))


def test_save_writes_clean_portable_records(fake: FakeWebUI, tmp_path: Path) -> None:
    s.save(_api(fake), tmp_path / "out", tmp_path / "stage")
    prompts = json.loads((tmp_path / "out" / "prompts.json").read_text())
    assert prompts == [{"command": "memo", "content": "Write", "name": "Memo"}]  # no id, user or timestamps
    tools = json.loads((tmp_path / "out" / "tools.json").read_text())
    assert "valves" not in tools[0]  # valves hold API keys
    assert json.loads((tmp_path / "out" / "knowledge.json").read_text()) == [{"name": "Policies",
                                                                             "description": "HR"}]
    assert not (tmp_path / "stage").exists()


def test_save_refuses_secrets_and_leaves_target_untouched(fake: FakeWebUI, tmp_path: Path) -> None:
    fake.prompts[0]["content"] = "use " + "sk-" + "Q" * 24
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "prompts.json").write_text("[]\n")
    with pytest.raises(s.StateError, match="possible secrets"):
        s.save(_api(fake), tmp_path / "out", tmp_path / "stage")
    assert (tmp_path / "out" / "prompts.json").read_text() == "[]\n"


def test_restore_adds_only_missing_items(fake: FakeWebUI) -> None:
    state = {"prompts": [{"command": "memo", "name": "Memo", "content": "Write"},
                         {"command": "brief", "name": "Brief", "content": "Summarise"}],
             "knowledge": [{"name": "Policies", "description": "HR"}, {"name": "Clients", "description": "CRM"}],
             "tools": [{"id": "fx", "name": "FX", "content": "code", "meta": {}}]}
    added = _api(fake).restore(state)
    assert added == {"models": 0, "prompts": 1, "tools": 0, "functions": 0, "knowledge": 1}
    assert _api(fake).restore(state) == dict.fromkeys(s.KINDS, 0)  # second run changes nothing
