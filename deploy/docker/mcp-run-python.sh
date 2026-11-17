#!/bin/sh
# Runs the PyPI package in $MCP_PACKAGE (one argv element, never parsed by a shell).
set -eu
: "${MCP_PACKAGE:?MCP_PACKAGE is required}"
if [ -n "${MCP_PIP_CONSTRAINTS:-}" ]; then
  # Comma separated specifiers become one constraint per line; uv validates every line.
  printf '%s\n' "$MCP_PIP_CONSTRAINTS" | tr ',' '\n' > /tmp/mcp-constraints.txt
  exec uvx --no-progress --constraints /tmp/mcp-constraints.txt -- "$MCP_PACKAGE"
fi
exec uvx --no-progress -- "$MCP_PACKAGE"
