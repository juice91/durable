# Platform runner for PyPI-published stdio MCP servers. MCP_PACKAGE (e.g. "mcp-server-time==1.2.3")
# is passed to uvx as a single argv element, never through a shell string. Cache, tool environments
# and HOME live under /tmp (an emptyDir under the read-only root filesystem).
FROM ghcr.io/astral-sh/uv:0.9.11-python3.13-bookworm-slim
RUN printf '%s\n' '#!/bin/sh' \
      ': "${MCP_PACKAGE:?MCP_PACKAGE is required}"' \
      'exec uvx --no-progress -- "$MCP_PACKAGE"' > /usr/local/bin/mcp-run \
    && chmod 0555 /usr/local/bin/mcp-run \
    && groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --home-dir /tmp --shell /usr/sbin/nologin mcp
ENV HOME=/tmp \
    UV_CACHE_DIR=/tmp/.uv-cache \
    UV_TOOL_DIR=/tmp/.uv-tools \
    UV_TOOL_BIN_DIR=/tmp/.uv-bin \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /tmp
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/mcp-run"]
