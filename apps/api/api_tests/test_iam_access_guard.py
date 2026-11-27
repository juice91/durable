"""IAM P2b guard: no request path without an Access.

Every FastAPI route, MCP control tool and AG-UI entry point must reach its resource services built WITH an `Access`
(`access=None` means "no caller to check"). Three checks, none of which needs a valid request body:

* routes: the dependency tree of every route is walked; each provider that returns a per-resource service
  (`AccessBound`) must depend on the per-request `Access`, and every other `*Service` a route reaches must be in
  `ACCESS_FREE_SERVICES` with its reason;
* MCP control: every tool and resource gets its services through `call_service`, which binds the caller's access to each
  `AccessBound` service, or is in `MCP_WITHOUT_SERVICE`;
* AG-UI: every handler that starts, reads or decides something asks `require_access` (or goes through an Access-built
  service).

Adding a route with a new service, or a tool that builds a store by hand, fails here until it is wired or listed."""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import eap_api.mcp_control as mcp_control_pkg
import pytest
from fastapi.routing import APIRoute

from eap_api.app import create_app
from eap_api.services.access import AccessBound

from .test_iam_p2b_wave1 import PG_URL

# Services a route may reach without an Access, with the reason. Keep this list small.
ACCESS_FREE_SERVICES: dict[str, str] = {
    "AuthzService": "the policy interface itself (capabilities ask what the caller may do; it decides nothing)",
    "IamCapabilitiesService": "reports the caller's own capabilities (`me`), computed through the policy engine",
    "PersonalApiKeyService": "person-only keys of the caller; checks the tenant membership through AuthzService itself",
    "IamExplainService": "explains a decision about the caller's own request through AuthzService; reads no resource",
    "LocalAuthService": "password sign-in before there is a caller: the identity plug-in verifies the password or the "
                        "refresh token; it reads no resource",
    "MembersService": "admin-only member management through the membership source; asks AuthzService itself",
    "RequestContextService": "request-context values of the caller's tenant (schema and usage), no resource",
    "ModelService": "the model catalog: platform configuration, not tenant data",
    "AuditService": "admin only: asks AuthzService for tenant.read_audit itself (`actor=me` is the caller's own rows)",
    "PermissionsReportService": "admin only: asks AuthzService for tenant.read_permissions itself",
    "SystemContextService": "filters every resource list through AuthzService.filter_ids, once per kind (design 8)",
    "ProvenanceService": "records the Architect run's own provenance and refuses every other principal (architect_only)",
    "CapabilityRegistryService": "the platform's capability registry; its dynamic entries (MCP server names and statuses) go "
                                 "through a pluggable EntryPolicy that P2c points at the engine (listed as left in docs/iam.md)",
    "WebhookIngestService": "the public hook route has no person to check: the authority is the trigger a person approved, "
                            "proven by its HMAC signature (the tenant comes from the trigger row)",
    "AgUiService": "the AG-UI entry points ask require_access in the handler (test_every_agui_handler_asks_the_policy)",
    "AgUiWorkflowService": "the AG-UI workflow entry points ask require_access in the handler (same test)",
}
# Routes with no tenant resource at all (health, auth, docs, the public descriptor). Method + path.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {}
PUBLIC_ROUTES[("GET", "/healthz")] = "liveness probe: no tenant data"
# The AG-UI routers: their handlers ask `require_access` (and the key's scopes and allowlists are constraints in the same call).
AGUI_MODULES = {"eap_api.routes.ag_ui", "eap_api.routes.ag_ui_workflows"}
AGUI_WITHOUT_ACCESS: dict[str, str] = {}
# MCP tools and resources that read nothing of the tenant's resources: `file:function` -> reason.
MCP_WITHOUT_SERVICE: dict[str, str] = {}


def _flatten(routes, prefix: str = ""):
    """The API routes of an app with their full paths: routers are included lazily (`_IncludedRouter`), so walk them."""
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _flatten(original.routes, prefix + (getattr(route.include_context, "prefix", "") or ""))
        elif isinstance(route, APIRoute):
            yield route, prefix + route.path


def _walk(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _walk(dep)


def _provider_return(call):
    try:
        ret = typing.get_type_hints(call).get("return")
    except Exception:  # noqa: BLE001 - an unresolvable annotation is reported below as "no class"
        return None
    return ret if inspect.isclass(ret) else None


@pytest.fixture
def routes():
    app = create_app(database_url=PG_URL)
    yield [route for route, _ in _flatten(app.routes)]
    app.state.database_engine.dispose() if hasattr(app.state, "database_engine") else None


def test_every_route_reaches_its_resource_services_through_an_access(routes):
    unchecked, unlisted = [], []
    for route in routes:
        calls = list(_walk(route.dependant))
        names = {getattr(c, "__name__", "") for c in calls}
        has_access = bool(names & {"get_access", "get_key_access"})
        for call in calls:
            ret = _provider_return(call)
            if ret is None:
                continue
            label = f"{sorted(route.methods)} {route.path} -> {call.__name__} ({ret.__name__})"
            if issubclass(ret, AccessBound):
                if not has_access:
                    unchecked.append(label)
            elif ret.__name__.endswith("Service") and ret.__name__ not in ACCESS_FREE_SERVICES:
                unlisted.append(label)
    assert not unchecked, "services built without an Access:\n" + "\n".join(sorted(set(unchecked)))
    assert not unlisted, "services with no Access and no reason in ACCESS_FREE_SERVICES:\n" + "\n".join(sorted(set(unlisted)))


def test_the_walk_sees_what_it_is_meant_to_see(routes):
    """The guard is only as good as its reading of the dependency tree: it must find the providers and resolve their types."""
    bound, free, unresolved = set(), set(), set()
    assert len(routes) > 150  # the whole API, not just the health check
    for route in routes:
        for call in _walk(route.dependant):
            name = getattr(call, "__name__", "")
            ret = _provider_return(call)
            if ret is None:
                if "service" in name.lower():
                    unresolved.add(name)
            elif issubclass(ret, AccessBound):
                bound.add(ret.__name__)
            elif ret.__name__.endswith("Service"):
                free.add(ret.__name__)
    assert not unresolved, f"provider return types the guard cannot resolve: {sorted(unresolved)}"
    print(sorted(bound), sorted(free))
    assert {"KnowledgeService", "SecretService", "EvalService", "TraceService", "McpServerService", "AgentService"} <= bound
    assert free <= set(ACCESS_FREE_SERVICES)


def test_routes_without_a_service_are_public_or_checked_in_the_handler(routes):
    """A route that reaches no service at all is public (listed) or an AG-UI handler (checked by the next test)."""
    loose = []
    for route in routes:
        services = [
            c for c in _walk(route.dependant)
            if _provider_return(c) is not None and _provider_return(c).__name__.endswith("Service")
        ]
        if services:
            continue
        key = (sorted(route.methods)[0], route.path)
        if key in PUBLIC_ROUTES or route.endpoint.__module__ in AGUI_MODULES:
            continue
        loose.append(f"{key} {route.endpoint.__module__}.{route.endpoint.__name__}")
    assert not loose, "routes with no service, not public and not AG-UI:\n" + "\n".join(loose)


def _handlers(module_name: str):
    """The route handlers of a module: functions decorated with `<router>.get / post / put / patch / delete`."""
    import importlib

    path = Path(importlib.import_module(module_name).__file__)
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {"get", "post", "put", "patch", "delete"}
            for d in node.decorator_list
        ):
            yield node


def _calls(node: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) in names for n in ast.walk(node)
    )


@pytest.mark.parametrize("module", sorted(AGUI_MODULES))
def test_every_agui_handler_asks_the_policy(module):
    """An AG-UI handler runs, reads or decides for a key or the dev bearer: it asks `require_access` (the key's own scope
    and allowlists are constraints evaluated in the same call), or is in AGUI_WITHOUT_ACCESS with its reason."""
    missing = [h.name for h in _handlers(module) if not _calls(h, {"require_access"}) and h.name not in AGUI_WITHOUT_ACCESS]
    assert not missing, f"{module}: handlers that never ask require_access: {missing}"
    assert list(_handlers(module)), "the guard found no handlers: it reads the module wrongly"


MCP_DIR = Path(mcp_control_pkg.__file__).parent


def _mcp_functions():
    """Every `@tool(...)` / `@resource(...)` function of the MCP control plane."""
    for path in sorted(MCP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(d, ast.Call) and getattr(d.func, "id", None) in {"tool", "resource"} for d in node.decorator_list
            ):
                yield path.name, node


def _outside_call_service(node: ast.AST) -> set[str]:
    """Names of stores, sessions and engines a tool touches OUTSIDE the arguments of `call_service*` (the one place that
    binds the caller's access to the services it builds)."""
    hits: set[str] = set()
    # A nested `def build(db)` handed to call_service by name is as good as a lambda written in its arguments.
    passed = {
        arg.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) in {"call_service", "call_service_async"}
        for arg in n.args if isinstance(arg, ast.Name)
    }

    def visit(n: ast.AST, inside: bool) -> None:
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) in {"call_service", "call_service_async"}:
            inside = True
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in passed:
            inside = True
        if not inside and isinstance(n, ast.Attribute) and n.attr in {"stores", "engine", "session", "db"}:
            hits.add(n.attr)
        if not inside and isinstance(n, ast.Name) and n.id in {"open_session", "run_stores_of", "database_engine_of"}:
            hits.add(n.id)
        for child in ast.iter_child_nodes(n):
            visit(child, inside)

    visit(node, False)
    return hits


# Helpers of the MCP package that go through call_service themselves (checked below).
MCP_SERVICE_HELPERS = {"tenant_run", "wait_for_run", "run_report"}


def test_the_mcp_helpers_go_through_call_service():
    found = set()
    for path in sorted(MCP_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in MCP_SERVICE_HELPERS:
                assert _calls(node, {"call_service", "call_service_async", "tenant_run"}), f"{path.name}:{node.name} builds no service through call_service"
                found.add(node.name)
    assert found == MCP_SERVICE_HELPERS


def test_every_mcp_tool_builds_its_services_through_call_service():
    functions = list(_mcp_functions())
    assert len(functions) > 40, "the guard found too few MCP tools: it reads the package wrongly"
    reaches, reads_store = [], []
    for filename, node in functions:
        key = f"{filename}:{node.name}"
        if key in MCP_WITHOUT_SERVICE:
            continue
        if not _calls(node, {"call_service", "call_service_async"} | MCP_SERVICE_HELPERS):
            reaches.append(key)
        touched = _outside_call_service(node)
        if touched:
            reads_store.append(f"{key} touches {sorted(touched)}")
    assert not reaches, "MCP tools that never use call_service (list them in MCP_WITHOUT_SERVICE with a reason):\n" + "\n".join(reaches)
    assert not reads_store, "MCP tools that reach a store or engine outside call_service:\n" + "\n".join(reads_store)
