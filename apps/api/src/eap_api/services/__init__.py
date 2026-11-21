"""API services: the business logic behind the routes.

The rule, enforced by the import-linter contract "API services have no web framework imports":

- A service holds business logic, independent of the transport: business rules and checks, orchestration
  across stores, transaction boundaries and hand-off to the worker, state transitions. Entry points (routes, mcp_control,
  ag_ui) never call a store: every store access goes through a service (a thin method is fine and expected
  for a simple read or write), and tenant scoping and authorization checks that are not HTTP-specific live
  here. A route parses the request, calls a service and renders its result. Not here: HTTP or protocol details (the controller), SQL
  (eap_run_store), pure shape validation (eap_schemas models), running agents (the worker).
- A service receives its collaborators (stores, a database session, the run launcher, settings) through
  its constructor. It never builds them and never looks them up.
- A service never imports `fastapi`, `starlette`, `eap_api.routes` or `eap_api.deps`, and never reads
  `app.state`: the providers in `eap_api.deps` are the only code that does, and they hand the
  collaborators to the service.
- A service refuses with a `ServiceError` subclass (`eap_api.services.errors`), never an `HTTPException`.
  One handler, `eap_api.service_errors.service_error_response`, turns it into the API's JSON error body.
"""
