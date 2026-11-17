# ToolHive operator

The managed MCP runtime uses the ToolHive Kubernetes operator only. Nothing in this directory is applied by
`kubectl apply -k deploy/k8s`: the operator is installed from two pinned OCI helm charts by
`deploy/scripts/toolhive.sh`, which `deploy/scripts/up.sh` calls after the cluster is up.

| Item | Value |
| --- | --- |
| Chart version | `0.65.0` (operator and CRDs) |
| CRDs chart | `oci://ghcr.io/stacklok/toolhive/toolhive-operator-crds` (release `toolhive-operator-crds`) |
| Operator chart | `oci://ghcr.io/stacklok/toolhive/toolhive-operator` (release `toolhive-operator`) |
| Namespace | `toolhive-system` |
| Probe CRD | `mcpservers.toolhive.stacklok.dev` (14 `toolhive.stacklok.dev` CRDs in total) |

```bash
deploy/scripts/toolhive.sh install   --context colima-pydanticos   # idempotent (helm upgrade --install)
deploy/scripts/toolhive.sh status    --context colima-pydanticos
deploy/scripts/toolhive.sh uninstall --yes --context colima-pydanticos   # releases, CRDs and the namespace
```

`--context` defaults to `colima-pydanticos`. On macOS the script installs helm with Homebrew when it is missing;
elsewhere it stops and asks for helm. The tenant `MCPServer` resources, Secrets, NetworkPolicies and quotas are
written at runtime by the platform's operator adapter, not by manifests here. Uninstalling the CRDs deletes every
`MCPServer`, so only do it on a cluster with no managed servers.
