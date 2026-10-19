"""MCP tool server for the Operator role: app, media, volume, Shortcuts and screenshot tools.

Started over stdio by OpenCode (`law mcp-mac`); see the `mcp` section of ~/LAW/config/opencode.json.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from law import mac_tools

server = MCPServer("law-mac")


@server.tool()
def open_app(name: str) -> str:
    """Open a Mac application by name, e.g. "Spotify", "Safari", "Notes"."""
    return mac_tools.open_app(name)


@server.tool()
def quit_app(name: str) -> str:
    """Quit a running Mac application by name."""
    return mac_tools.quit_app(name)


@server.tool()
def open_url(url: str) -> str:
    """Open a URL in the default browser (or an app link such as spotify:...)."""
    return mac_tools.open_url(url)


@server.tool()
def get_volume() -> int:
    """Current output volume, 0-100."""
    return mac_tools.get_volume()


@server.tool()
def set_volume(percent: int) -> str:
    """Set the output volume, 0-100."""
    return mac_tools.set_volume(percent)


@server.tool()
def media(app: str, action: str) -> str:
    """Control Spotify or Music: action is play, pause, playpause, next or previous."""
    return mac_tools.media(app, action)


@server.tool()
def play_search(app: str, query: str) -> str:
    """Search and play in Spotify or Music; returns what is playing afterwards."""
    return mac_tools.play_search(app, query)


@server.tool()
def now_playing(app: str) -> str:
    """What Spotify or Music is playing right now."""
    return mac_tools.now_playing(app)


@server.tool()
def run_shortcut(name: str, input_text: str | None = None) -> str:
    """Run a macOS Shortcut by name, optionally passing text input."""
    return mac_tools.run_shortcut(name, input_text)


@server.tool()
def applescript(script: str) -> str:
    """Run AppleScript for apps with a scripting dictionary; returns the script's result."""
    return mac_tools.osascript(script)


@server.tool()
def screenshot() -> str:
    """Capture the screen; returns the PNG path so a vision model can check the result."""
    return mac_tools.screenshot()


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
