# Platform runner for npm-published stdio MCP servers. The package reference arrives as the env
# variable MCP_PACKAGE (e.g. "@scope/name@1.2.3"); it is passed to npx as a single argv element and
# never goes through a shell string. The pod runs with a read-only root filesystem, so the npm cache
# and HOME live under /tmp, which the operator mounts as an emptyDir.
FROM node:22.21.1-bookworm-slim
RUN printf '%s\n' '#!/bin/sh' \
      ': "${MCP_PACKAGE:?MCP_PACKAGE is required}"' \
      'exec npx --yes -- "$MCP_PACKAGE"' > /usr/local/bin/mcp-run \
    && chmod 0555 /usr/local/bin/mcp-run \
    && groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --home-dir /tmp --shell /usr/sbin/nologin mcp
ENV HOME=/tmp \
    npm_config_cache=/tmp/.npm \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false
WORKDIR /tmp
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/mcp-run"]
