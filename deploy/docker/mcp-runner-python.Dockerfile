# Platform runner for PyPI-published stdio MCP servers. MCP_PACKAGE (e.g. "mcp-server-time==1.2.3")
# is passed to uvx as a single argv element, never through a shell string. Cache, tool environments
# and HOME live under /tmp (an emptyDir under the read-only root filesystem). MCP_PIP_CONSTRAINTS
# (optional, comma separated requirement specifiers such as "mcp<2") is written to a constraints file
# for uv, so a transitive dependency that breaks a server can be held back without rebuilding.
FROM ghcr.io/astral-sh/uv:0.9.11-python3.13-bookworm-slim
COPY mcp-run-python.sh /usr/local/bin/mcp-run
RUN chmod 0555 /usr/local/bin/mcp-run \
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
