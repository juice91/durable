# eap-mcp-runtime

`McpRuntime` runs a tenant's managed MCP server as an isolated pod. `OperatorRuntime` writes a
`MCPServer` custom resource (the ToolHive operator does the container lifecycle and the stdio to
streamable-HTTP proxy), a Kubernetes `Secret`, a `NetworkPolicy` and a `ResourceQuota` in the
tenant's namespace `mcp-tenant-<hash12(tenant_id)>`. No web, database, agent or platform imports.

Contract tests (`requires_k3s`) run against the local `colima-pydanticos` cluster and skip with a
reason when it is unreachable; `EAP_REQUIRE_K3S=1` makes them fail instead.
