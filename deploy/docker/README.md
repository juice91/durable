# Docker images

| File | Image | Purpose |
| --- | --- | --- |
| `python.Dockerfile` | `pydanticos-python:<git sha>` | API, worker and migrations |
| `studio.Dockerfile` | `pydanticos-studio:<git sha>` | Studio (Next.js) |
| `mcp-runner-node.Dockerfile` | `pydanticos-mcp-runner-node:<content hash>` | runs an npm-published stdio MCP server |
| `mcp-runner-python.Dockerfile` (+ `mcp-run-python.sh`) | `pydanticos-mcp-runner-python:<content hash>` | runs a PyPI-published stdio MCP server |

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
  tag of the default names with `EAP_MCP_RUNNER_TAG`.
- The default tag is the first 12 hex characters of the SHA-256 of the files the image is built from
  (node: `mcp-runner-node.Dockerfile`; python: `mcp-runner-python.Dockerfile` then `mcp-run-python.sh`,
  concatenated). `up.sh` and `eap_mcp_runtime.images` compute it the same way, so the tag changes
  only when the image does: a code commit neither retags nor forces a rebuild, and the contract tests
  find the images `up.sh` built. After editing a runner file, run `up.sh` (or build and tag the image
  yourself) before the contract tests; `local` is the tag when the files are not found.
- The python runner honours `MCP_PIP_CONSTRAINTS` (comma separated specifiers, e.g. `mcp<2`) as a uv
  constraints file, to hold back a transitive dependency that breaks a server without a rebuild. Set
  it in the server's non-secret env.
- First start downloads the package from the registry, so a cold start takes seconds longer than an
  `oci` server; later starts of the same pod reuse nothing (the cache is per pod).
