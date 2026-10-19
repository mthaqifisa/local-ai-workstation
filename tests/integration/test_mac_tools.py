"""Live checks of the Mac app-control helpers on this machine (restores everything it changes)."""

from __future__ import annotations

import os

import pytest

from law import mac_tools

pytestmark = pytest.mark.integration


def test_volume_round_trip_restores_original() -> None:
    original = mac_tools.get_volume()
    try:
        mac_tools.set_volume(30)
        assert mac_tools.get_volume() == 30
    finally:
        mac_tools.set_volume(original)
    assert mac_tools.get_volume() == original


def test_screenshot_is_written_then_removed(tmp_path) -> None:
    path = mac_tools.screenshot(tmp_path)
    assert os.path.getsize(path) > 10_000
    os.remove(path)


def test_open_and_quit_textedit() -> None:
    mac_tools.open_app("TextEdit")
    assert "TextEdit" in mac_tools.osascript('tell application "System Events" to name of every process')
    mac_tools.quit_app("TextEdit")
