"""Contract tests: LAW's parsers against real recorded `opencode run --format json` streams: v1/ is OpenCode
1.18.7 (live), v2/ is OpenCode 2.4.0 (tried isolated, not adopted: ADR-0028).

Each fixture was recorded from a live coder run (write a file, run it, edit it, answer) with the home folder replaced
by /srv/fixture-home. When OpenCode is upgraded, record the same run with the new version in a new folder; these tests
show at once whether progress lines, the stall guard, the session id and the final answer still parse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from law.handlers.tasks import (
    describe_opencode_line,
    in_law_folder,
    is_code_change,
    is_file_change,
    parse_opencode_events,
    session_id,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "opencode"
STREAMS = sorted(FIXTURES.glob("v*/coder-run.jsonl"))


@pytest.fixture(params=STREAMS, ids=lambda p: p.parent.name)
def stream(request: pytest.FixtureRequest) -> str:
    return Path(request.param).read_text()


def test_fixture_present() -> None:
    assert STREAMS, "no recorded OpenCode stream"


def test_every_line_is_an_event_with_a_type(stream: str) -> None:
    for line in stream.splitlines():
        ev = json.loads(line)
        assert ev["type"] and ev["sessionID"] and isinstance(ev.get("part"), dict)


def test_progress_lines_name_each_tool_call(stream: str) -> None:
    lines = [d for d in map(describe_opencode_line, stream.splitlines()) if d]
    assert [d.split(" ", 1)[0] for d in lines] == ["✏️", "▶", "✏️", "▶"]
    assert lines[0].endswith("add.py") and "writing" in lines[0]
    assert lines[2].endswith("add.py") and "editing" in lines[2]
    assert "python3 -c 'import add; print(add.add(2,3))'" in lines[1]


def test_stall_guard_sees_the_two_file_changes(stream: str) -> None:
    lines = stream.splitlines()
    assert sum(map(is_file_change, lines)) == 2
    assert sum(map(is_code_change, lines)) == 2


def test_session_id_and_final_answer(stream: str) -> None:
    sid = session_id(stream)
    assert sid and sid.startswith("ses_")
    assert parse_opencode_events(stream) == "5"


def test_unfinished_tool_calls_are_not_progress(stream: str) -> None:
    line = next(ln for ln in stream.splitlines() if '"tool_use"' in ln)
    ev = json.loads(line)
    ev["part"]["state"]["status"] = "running"
    assert describe_opencode_line(json.dumps(ev)) is None


@pytest.mark.parametrize(("summary", "inside"), [
    ("✏️ editing ~/SourceCode/app/.law/PLAN.md", True),
    ("✏️ editing .law/PLAN.md", True),  # OpenCode 2 writes paths relative to the project
    ("📖 reading .law/BUGS.md", True),
    ("✏️ editing src/law/app.py", False),
    ("✏️ editing notes.law/x.md", False),
])
def test_law_folder_absolute_or_relative(summary: str, inside: bool) -> None:
    assert in_law_folder(summary) is inside
