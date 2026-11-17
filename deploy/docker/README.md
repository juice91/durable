# Docker images

| File | Image | Purpose |
| --- | --- | --- |
| `python.Dockerfile` | `pydanticos-python:<git sha>` | API, worker and migrations |
| `studio.Dockerfile` | `pydanticos-studio:<git sha>` | Studio (Next.js) |
| `mcp-runner-node.Dockerfile` | `pydanticos-mcp-runner-node:<git sha>` | runs an npm-published stdio MCP server |
| `mcp-runner-python.Dockerfile` | `pydanticos-mcp-runner-python:<git sha>` | runs a PyPI-published stdio MCP server |

`deploy/scripts/up.sh` builds all four into the `pydanticos` profile's Docker, so k3s sees them
without a registry.

## MCP runner images

The ToolHive operator cannot take `npx://` or `uvx://` packages, so npm and PyPI servers run on
these two pinned images, which install the package when the pod starts.

- The package reference is the env variable `MCP_PACKAGE` (`name@version` for npm,
  `name==version` for PyPI). The entrypoint `/usr/local/bin/mcp-run` passes it to `npx` or `uvx`
  as one argument after `--`; it is never built into a shell string, and
  `eap_mcp_runtime.images.image_for` rejects identifiers or versions that are not plain names.
- Non-root (uid/gid 10001). `HOME`, the npm cache (`npm_config_cache`) and the uv cache, tool and
  bin directories all live under `/tmp`; the operator mounts an `emptyDir` there and keeps the
  root filesystem read-only.
- Override the images with `EAP_MCP_RUNNER_NODE_IMAGE` and `EAP_MCP_RUNNER_PYTHON_IMAGE`, or the
  tag of the default names with `EAP_MCP_RUNNER_TAG` (default: the git SHA).
- First start downloads the package from the registry, so a cold start takes seconds longer than an
  `oci` server; later starts of the same pod reuse nothing (the cache is per pod).
