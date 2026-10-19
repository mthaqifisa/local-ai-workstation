"""Start `law mcp-mac` as OpenCode does (stdio) and talk to it with a real MCP client."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.integration

EXPECTED = {"open_app", "quit_app", "open_url", "get_volume", "set_volume", "media", "play_search",
            "now_playing", "run_shortcut", "applescript", "screenshot"}


async def test_mcp_server_lists_tools_and_reads_volume() -> None:
    law_bin = Path(sys.executable).parent / "law"
    params = StdioServerParameters(command=str(law_bin), args=["mcp-mac"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        assert tools == EXPECTED
        result = await session.call_tool("get_volume", {})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert text.strip().isdigit() and 0 <= int(text.strip()) <= 100
