#!/bin/sh
# Starts Findling's stdio bridge (findling-mcp) for Claude Desktop.
# Claude Desktop runs this with a minimal PATH, so look for uv in the usual places.
# Messages for the user go to stderr (Claude Desktop's MCP log); stdout is the MCP wire.

if [ -z "$FINDLING_DIR" ] || [ ! -f "$FINDLING_DIR/src/findling/mcp_stdio.py" ]; then
  echo "findling: '$FINDLING_DIR' isn't the Findling folder (no src/findling/mcp_stdio.py). Fix it in the extension's settings." >&2
  exit 1
fi

uv="$FINDLING_UV"
if [ -z "$uv" ]; then
  for candidate in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" /opt/homebrew/bin/uv \
      /usr/local/bin/uv "$HOME/.cargo/bin/uv"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      uv="$candidate"
      break
    fi
  done
fi
if [ -z "$uv" ] || [ ! -x "$uv" ]; then
  echo "findling: uv not found. Install it (https://docs.astral.sh/uv/) or set its path in the extension's settings." >&2
  exit 1
fi

exec "$uv" run --quiet --directory "$FINDLING_DIR" findling-mcp
