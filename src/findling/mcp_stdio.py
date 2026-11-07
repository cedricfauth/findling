"""Stdio bridge to Findling's MCP server, for clients that start MCP servers as a
command (Claude Desktop and others):

    uv run --directory /path/to/findling findling-mcp

Forwards tool listings and calls to the running web app (uv run findling) at
http://127.0.0.1:$FINDLING_PORT/mcp (or $FINDLING_MCP_URL). It doesn't load the model
or open the index itself, so it starts in about a second and shares the web app's
live index. If the web app isn't running, the tools are still listed and calls
explain how to start it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import urllib.parse

import mcp_types as types
from mcp import Client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import mcp_server

PORT = int(os.environ.get("FINDLING_PORT") or "8765")
URL = os.environ.get("FINDLING_MCP_URL") or f"http://127.0.0.1:{PORT}/mcp"
TIMEOUT = 120  # seconds; a search takes well under that, even on old Macs

log = logging.getLogger("findling.mcp_stdio")


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def _server_up() -> bool:
    """Quick check that something listens at URL (avoids a slow, noisy failed connect)."""
    parts = urllib.parse.urlsplit(URL)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


NOT_RUNNING = (
    "Findling isn't running, so its index can't be searched. Ask the user to start it "
    "(in the findling folder: uv run findling) and try again."
)


async def list_tools(_ctx, _params) -> types.ListToolsResult:
    if await asyncio.to_thread(_server_up):
        try:
            async with Client(URL, read_timeout_seconds=TIMEOUT) as client:
                return await client.list_tools()
        except Exception as e:
            log.warning("listing tools at %s failed: %s", URL, e)
    # Describe the tools anyway (same code as the server's), so the client knows them;
    # calls then say how to start Findling.
    return types.ListToolsResult(tools=await mcp_server.create_server(None).list_tools())


async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    if not await asyncio.to_thread(_server_up):
        return _error(NOT_RUNNING)
    try:
        async with Client(URL, read_timeout_seconds=TIMEOUT) as client:
            return await client.call_tool(params.name, params.arguments or {})
    except Exception as e:
        log.exception("calling %s at %s failed", params.name, URL)
        return _error(f"Findling's server at {URL} didn't answer: {e}")


async def serve() -> None:
    server = Server(
        "findling",
        title="Findling local search",
        instructions=mcp_server.INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    # stdout carries the MCP messages; logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
