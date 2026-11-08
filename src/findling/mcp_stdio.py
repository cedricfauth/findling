"""Stdio bridge to Findling's MCP server, for clients that start MCP servers as a
command (Claude Desktop and others):

    findling-mcp

Forwards tool listings and calls to the running web app (findling) at
http://127.0.0.1:$FINDLING_PORT/mcp (or $FINDLING_MCP_URL). It doesn't load the model
or open the index itself, so it starts in about a second and shares the web app's
live index. If the web app isn't running, it starts it in the background (log in
~/Library/Logs/Findling/findling.log, on Linux ~/.local/state/findling/findling.log);
it keeps running after the client quits.
"""

from __future__ import annotations

import asyncio
import fcntl
import importlib.metadata
import json
import logging
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import mcp_types as types
from mcp import Client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import mcp_server

PORT = int(os.environ.get("FINDLING_PORT") or "8765")
URL = os.environ.get("FINDLING_MCP_URL") or f"http://127.0.0.1:{PORT}/mcp"
TIMEOUT = 120  # seconds; a search takes well under that, even on old Macs
STARTUP_TIMEOUT = 120  # seconds to wait for a starting web app (model load: ~20 s)
LOG_PATH = (
    pathlib.Path.home() / "Library/Logs/Findling/findling.log"
    if sys.platform == "darwin"
    else pathlib.Path(os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local/state")
    / "findling/findling.log"
)
LOCK_PATH = LOG_PATH.with_name("findling.lock")
SERVER_COMMAND = "-m findling.server"  # how _spawn starts it (see _restart_if_outdated)
VERSION = importlib.metadata.version("findling")

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
    f"Findling isn't running at {URL}, so its index can't be searched. Ask the user to "
    "start it (findling) and try again."
)
STARTING = "Findling is still starting up. Try again in a moment."


def _locked():
    """Held while starting or restarting the web app: bridges start together (Claude
    Desktop starts two), and none may start one while an old one is still stopping."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "a")
    fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes
    return lock


def _spawn() -> None:
    """Start the web app in its own session, so it isn't stopped with this bridge
    (the client quitting kills our process group). Returns once it listens (or
    exited), so other bridges waiting for the lock see it."""
    with open(LOG_PATH, "ab") as out:
        proc = subprocess.Popen(
            [sys.executable, *SERVER_COMMAND.split()],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not _server_up() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)


def _start_server() -> bool:
    """Start the web app unless it's running. False if it isn't ours to start."""
    if "FINDLING_MCP_URL" in os.environ:  # someone else's server; don't start ours
        return False
    with _locked():
        if not _server_up():
            _spawn()
    return True


def _ready(status: dict | None) -> bool:
    """Ready for tool calls: the model is loaded, or it failed (calls then say why)."""
    return status is not None and (status["model_ready"] or status["indexing"]["phase"] == "error")


def _status() -> dict | None:
    try:
        with urllib.request.urlopen(urllib.parse.urljoin(URL, "/api/status"), timeout=5) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def _wait_until_ready() -> str | None:
    """Start the web app if needed and wait for it. None when ready, else the error."""
    if not _server_up() and not _start_server():
        return NOT_RUNNING
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not _ready(_status()):
        if time.monotonic() > deadline:
            return STARTING if _server_up() else f"{NOT_RUNNING} (See {LOG_PATH}.)"
        time.sleep(1)
    return None


def _command(pid: int) -> str:
    """The process's command line ("" once it's gone)."""
    ps = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return ps.stdout.strip()


def _restart_if_outdated() -> None:
    """After an upgrade, replace the web app that an older bridge started (it keeps
    running after Claude quits). Locked, so bridges starting together restart it once."""
    if "FINDLING_MCP_URL" in os.environ:  # someone else's server
        return
    with _locked():
        status = _status()
        if status is None or status.get("version") == VERSION:
            return
        pid = status.get("pid")
        # Only a server started by a bridge: not one in a terminal, nor any other process.
        if pid is None or SERVER_COMMAND not in _command(pid):
            log.warning(
                "Findling %s is running, this is %s. Restart Findling to use the new version.",
                status.get("version", "(older)"), VERSION,
            )
            return
        log.warning("restarting Findling %s (pid %d) as %s", status["version"], pid, VERSION)
        # SIGTERM = Ctrl-C: the server stops indexing cleanly (finished files are kept).
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        # Wait for the process, not the port: it closes the port before it stops indexing.
        while SERVER_COMMAND in _command(pid):
            if time.monotonic() > deadline:
                log.warning("Findling (pid %d) didn't stop; still using it", pid)
                return
            time.sleep(0.5)
        _spawn()


async def list_tools(_ctx, _params) -> types.ListToolsResult:
    if await asyncio.to_thread(_server_up):
        try:
            async with Client(URL, read_timeout_seconds=TIMEOUT) as client:
                return await client.list_tools()
        except Exception as e:
            log.warning("listing tools at %s failed: %s", URL, e)
    else:
        # Clients list tools when they start: load the model now, before the first call.
        await asyncio.to_thread(_start_server)
    # Describe the tools anyway (same code as the server's), so the client knows them;
    # calls wait for the web app.
    return types.ListToolsResult(tools=await mcp_server.create_server(None).list_tools())


async def call_tool(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    if problem := await asyncio.to_thread(_wait_until_ready):
        return _error(problem)
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
    # In the background: the client expects an answer to initialize within seconds.
    restarting = asyncio.create_task(asyncio.to_thread(_restart_if_outdated))  # noqa: F841 (keep a reference)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    # stdout carries the MCP messages; logs go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
