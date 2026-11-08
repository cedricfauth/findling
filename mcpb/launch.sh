#!/bin/sh
# Starts Findling's stdio bridge (findling-mcp) for Claude Desktop.
# Claude Desktop runs this with a minimal PATH, so also look where `uv tool install` puts it.
# Messages for the user go to stderr (Claude Desktop's MCP log); stdout is the MCP wire.

for candidate in "$(command -v findling-mcp 2>/dev/null)" "${XDG_BIN_HOME:-$HOME/.local/bin}/findling-mcp"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    exec "$candidate"
  fi
done
echo "findling: findling-mcp not found. Install Findling in Terminal with: uv tool install git+https://github.com/CedricFauth/findling" >&2
exit 1
