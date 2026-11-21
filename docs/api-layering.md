# API layering

The API is built in four layers. Each rule below is checked by a tool, not by memory.

| Layer | Where | What belongs there |
|---|---|---|
| Controller | `eap_api/routes/`, and the other entry points `eap_api/mcp_control/` and `eap_api/ag_ui/` | Protocol only; never calls a store. REST: parse the request, headers, ETag / If-Match, streaming, status codes. MCP control and AG-UI are controllers too: they parse their protocol, call a service or a store, and render the answer. |
| Service | `eap_api/services/` | Business logic, independent of the transport: business rules and checks (publish guards such as the model allowlist and "MCP server exists"; at most 10 scorers per agent), orchestration across stores, transaction boundaries and hand-off to the worker, state transitions (an approval only moves from pending to approved, rejected or expired). Collaborators (stores, session, launcher, settings) come through the constructor. It refuses with a `ServiceError` subclass (`NotFound`, `Conflict`, `InvalidInput`, `Forbidden`, `Unavailable`), never `HTTPException`. It never imports `fastapi` or `starlette` and never reads `app.state`. Not in a service: HTTP or protocol details (controller), SQL (`eap_run_store`), pure shape validation (`eap_schemas` Pydantic models), running agents (worker). |
| Data access | `eap_run_store` | All SQL. Every query filters by tenant. Reached only from services and `deps.py`. |
| Dependency providers | `eap_api/deps.py`, `eap_api/service_deps/<domain>.py` | `deps.py`: `get_<name>(request)` and `<Name>Dep` for every long-lived object (stores, engine, launcher); the only reader of `app.state` besides `app.py`, the composition root. `service_deps/<domain>.py`: `get_<x>_service(...)` and `<X>ServiceDep`, which build a service from those providers (`deps.py` cannot import services). A test replaces a provider with `app.dependency_overrides[get_<name>]`. |

`service_errors.py` holds the one translator from `ServiceError` to the API's JSON error body
(`{"detail", "code"}`, plus `errors`; a stale revision answers 412 with `current` and the ETag).

## Rules

- A service raises `ServiceError` subclasses, or the existing `DefinitionError` (`eap_run_store.definition_errors`)
  when an existing response body needs a list-valued `detail` or no `code` key (`ServiceError(detail_object=...)`
  also answers `{"detail": <object>}` exactly).
- A service imports `eap_observability` submodules directly (`eap_observability.<module>`), never the package root:
  the root imports the HTTP middleware.
- Strict layering: controller -> service -> `eap_run_store`. Entry points never call a store. A thin service
  method is fine and expected for simple reads and writes. Tenant scoping and authorization checks that are
  not HTTP-specific live in the service.
- Providers build the stores (`eap_api/deps.py`, which may import `eap_run_store`) and the services
  (`eap_api/service_deps/<domain>.py`) and hand them to the entry points.
- New code: business logic (a rule, more than one store, a state transition, a worker hand-off) and every
  store call goes into a service.
- Existing code: no big-bang move. Any route change that touches a store call moves that call behind a
  service. Needing two or more entry points (MCP control, Architect) is only the signal for which existing
  logic to move first, not the definition of a service.
- A large route file is split by sub-resource only when parallel work needs it.
- A module on a ratchet list below leaves it when it is clean. An entry point that imports only a constant
  or a type from `eap_run_store` (e.g. `TERMINAL_RUN_STATUSES`) is on the list for now; re-exporting those
  through `eap_schemas` or the service module is acceptable later.

## Enforcing checks

- `uv run lint-imports` (root `pyproject.toml`):
  - "API services have no web framework imports": `eap_api.services` must not import `fastapi`, `starlette`,
    `eap_api.routes` or `eap_api.deps`.
  - "API dependency providers do not import entry points": `eap_api.deps` must not import `eap_api.routes`,
    `eap_api.mcp_control`, `eap_api.ag_ui` or `eap_api.services`.
  - "API entry points reach data only through services": the same three packages must not import
    `eap_run_store`; same ratchet rule for its `ignore_imports`.
  - "Service providers do not import entry points": `eap_api.service_deps` must not import `eap_api.routes`,
    `eap_api.mcp_control` or `eap_api.ag_ui`.
  - "API entry points do not query the database": `eap_api.routes`, `eap_api.mcp_control` and `eap_api.ag_ui`
    must not import `sqlalchemy`. Modules that did when the rule landed are listed in `ignore_imports`.
    That list is a ratchet: remove a line when a module is clean, never add one. A new module fails lint if
    it imports `sqlalchemy`.
- `apps/api/api_tests/test_architecture_app_state.py` parses every module under `eap_api` and fails on any
  `app.state` access outside `app.py` and `deps.py`.

## Reviewer checklist

API layering: entry points call services only (no new eap_run_store / sqlalchemy / app.state imports in routes, mcp_control, ag_ui); business logic in services; route functions only do HTTP.
