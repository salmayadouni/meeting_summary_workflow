"""
mcp_client.py — Thin async helper for calling tools on a FastMCP server.

Usage:
    from mcp_client import call_mcp

    result = await call_mcp("http://localhost:8082/mcp", "create_calendar_event", {
        "title": "Sprint Review",
        "date":  "2026-05-12",
        ...
    })
"""

import json
import logging

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


async def call_mcp(server_url: str, tool_name: str, arguments: dict) -> dict | list:
    """
    Connect to an MCP server, call one tool, and return the parsed result.

    Args:
        server_url: Full URL of the MCP server, e.g. "http://localhost:8082/mcp".
        tool_name:  Name of the tool to call.
        arguments:  Dict of arguments to pass to the tool.

    Returns:
        Parsed JSON result from the tool (dict or list).

    Raises:
        RuntimeError if the server returns an error or no content.
    """
    async with streamablehttp_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            logger.debug("MCP call → %s::%s(%s)", server_url, tool_name, arguments)
            result = await session.call_tool(tool_name, arguments)

            if result.isError:
                error_text = result.content[0].text if result.content else "unknown error"
                raise RuntimeError(f"MCP tool '{tool_name}' returned error: {error_text}")

            if not result.content:
                return {}

            raw = result.content[0].text
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"raw": raw}
