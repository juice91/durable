# MCP servers: idle stop, Test and keep-running

A managed MCP server (one the platform runs for you, installed from the catalog) is a pod in the cluster. To save
resources the worker's runtime sweep (every minute) stops a managed server that nobody has used for
`EAP_MCP_IDLE_MINUTES` minutes (default 30, 1 to 10080). Its objects stay; only the pod is scaled down. The server
list shows the state as a pill: **Running**, **Stopped (idle)**, **Starting** or **Failed**.

## Test starts a stopped server

The **Test connection** button (`POST /mcp-servers/{id}/test`) starts a stopped managed server and waits for it, up to
`EAP_MCP_TEST_START_TIMEOUT_SECONDS` (default 60, at most 300), then runs the test. The answer carries
`runtime: {state, started, waited_ms, idle_minutes}` (`state` is the state before the test). Studio says:

- Started in time: "Started the server (idle for 30 min) and tested it: OK".
- Not ready in time (`error_code: "runtime_starting"`): "Server is starting, test again in a moment", with a Retry button.
- A runtime in `failed` (`runtime_not_running`, with `runtime.last_error`) and every other code: "Connection failed".

A `runtime_starting` answer is not stored as the server's last test result. An agent run also starts a stopped server
on demand; the first call after an idle stop is slower by the pod's start time.

## Keep running

Edit a managed server and tick **Keep running (never stop when idle)** (`PATCH /mcp-servers/{id}` with
`{"keep_warm": true}`, managed servers only; stored as `mcp_servers.keep_warm`). The sweep skips its runtime however
long it has been idle. The cost is a pod that runs all the time.
