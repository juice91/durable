"""ETag / If-Match on the definition-class resources (migration 0033, eap_api.concurrency).

One scenario matrix runs against every PATCH route in scope: GET carries the ETag and `revision`; a
matching `If-Match` writes and answers the next ETag; a stale one is 412 `revision_mismatch` carrying the
current resource (and writes nothing); a missing one is allowed with the transitional `Warning`;
`EAP_REQUIRE_IF_MATCH=1` turns it into 428; `*` and the bare integer are accepted.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.concurrency import WARNING_MISSING
from eap_api.routes.mcp_servers import get_outbound_policy
from eap_kb_sources.fake import FakeCatalogAdapter
from eap_kb_sources.registry import register_adapter
from eap_secrets import KEY_ENV, generate_key
from eap_secrets.outbound import OutboundPolicy

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_evals_api import AGENT_SPEC, _agent, _suite
from .test_workflow_runs_api import publish_demo

pytestmark = requires_pg
API = "/api/v1"
TABLES = ("workflow_schedules", "eval_suites", "scorers", "kb_sources", "knowledge_bases", "settings_connections",
          "mcp_servers", "workflow_versions", "workflows")


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    monkeypatch.delenv("EAP_REQUIRE_IF_MATCH", raising=False)
    register_adapter("product_catalog", "fake", lambda: FakeCatalogAdapter())
    app = create_app(database_url=PG_URL)

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=resolve)
    tenants: list[str] = []

    def tenant_of() -> dict[str, str]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return get_auth_header(t, "alice")

    with hard_timeout(120), TestClient(app) as client:
        yield client, tenant_of
    engine = app.state.database_engine
    for t in tenants:
        with engine.begin() as conn:
            for table in TABLES:
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": t})
        cleanup_tenant(engine, t)
    engine.dispose()


# Each maker returns (url, patch_body(n)): n makes a distinct, valid change.
Maker = Callable[[TestClient, dict[str, str]], tuple[str, Callable[[int], dict[str, Any]]]]


def _agent_resource(client, h):
    agent_id = _agent(client, h, publish=False)
    return f"{API}/agents/{agent_id}", lambda n: {"spec": {**AGENT_SPEC, "instructions": f"v{n}"}}


def _workflow_resource(client, h):
    workflow_id = publish_demo(client, h)
    draft = client.get(f"{API}/workflows/{workflow_id}", headers=h).json()["draft_spec"]
    return f"{API}/workflows/{workflow_id}", lambda n: {"spec": {**draft, "name": f"Triage {n}"}}


def _schedule_resource(client, h):
    workflow_id = publish_demo(client, h)
    made = client.post(
        f"{API}/workflows/{workflow_id}/schedules",
        json={"name": "hourly", "cron": "5 * * * *", "timezone": "UTC", "input": {"q": "x"}}, headers=h)
    assert made.status_code == 201, made.text
    return f"{API}/workflows/schedules/{made.json()['id']}", lambda n: {"cron": f"{n % 50 + 1} * * * *"}


def _suite_resource(client, h):
    suite_id = _suite(client, h, _agent(client, h), cases=())
    return f"{API}/eval-suites/{suite_id}", lambda n: {"description": f"d{n}"}


def _case_resource(client, h):
    suite_id = _suite(client, h, _agent(client, h), cases=("a",))
    case_id = client.get(f"{API}/eval-suites/{suite_id}/cases", headers=h).json()["items"][0]["id"]
    return f"{API}/eval-suites/{suite_id}/cases/{case_id}", lambda n: {"input": f"i{n}"}


def _scorer_resource(client, h):
    made = client.post(f"{API}/scorers", json={"name": "polite", "evaluator": {"kind": "contains", "value": "hi"}},
                       headers=h)
    assert made.status_code == 201, made.text
    return f"{API}/scorers/{made.json()['id']}", lambda n: {"description": f"d{n}"}


def _connection_resource(client, h):
    made = client.post(f"{API}/settings/connections", json={
        "name": "ti", "kind": "oauth2_client_credentials", "base_url": "https://transact.example.com",
        "config": {"provider": "fake"}, "credential_refs": {"client_id": "secret:a", "client_secret": "secret:b"}},
        headers=h)
    assert made.status_code == 201, made.text
    return f"{API}/settings/connections/{made.json()['id']}", lambda n: {"name": f"ti-{n}"}


def _kb_source_resource(client, h):
    conn = client.post(f"{API}/settings/connections", json={
        "name": "ti", "kind": "oauth2_client_credentials", "base_url": "https://transact.example.com",
        "config": {"provider": "fake"}, "credential_refs": {"client_id": "secret:a", "client_secret": "secret:b"}},
        headers=h).json()
    kb = client.post(f"{API}/knowledge-bases", json={"name": "docs", "embedding_model": "test:hash"}, headers=h).json()
    made = client.post(f"{API}/knowledge-bases/{kb['id']}/sources", json={
        "connection_id": conn["id"], "name": "parts", "kind": "product_catalog", "provider": "fake", "config": {}},
        headers=h)
    assert made.status_code == 201, made.text
    return f"{API}/knowledge-bases/{kb['id']}/sources/{made.json()['id']}", lambda n: {"name": f"parts-{n}"}


def _mcp_resource(client, h):
    made = client.post(f"{API}/mcp-servers", json={"name": "docs", "url": "https://mcp.example.com/mcp"}, headers=h)
    assert made.status_code == 201, made.text
    return f"{API}/mcp-servers/{made.json()['id']}", lambda n: {"name": f"docs-{n}"}


RESOURCES: dict[str, Maker] = {
    "agent": _agent_resource, "workflow": _workflow_resource, "schedule": _schedule_resource,
    "eval_suite": _suite_resource, "eval_case": _case_resource, "scorer": _scorer_resource,
    "connection": _connection_resource, "kb_source": _kb_source_resource, "mcp_server": _mcp_resource,
}


def _rev(response) -> int:
    return response.json()["revision"]


@pytest.mark.parametrize("kind", RESOURCES)
def test_etag_if_match_matrix(world, monkeypatch, kind):
    client, tenant_of = world
    h = tenant_of()
    url, body = RESOURCES[kind](client, h)

    # GET: the ETag is the revision, which is also in the body.
    first = client.get(url, headers=h)
    assert first.status_code == 200, first.text
    revision = _rev(first)
    assert first.headers["ETag"] == f'"{revision}"' and revision >= 1

    # a matching If-Match writes and answers the next ETag, without the transitional warning
    ok = client.patch(url, json=body(1), headers={**h, "If-Match": first.headers["ETag"]})
    assert ok.status_code == 200, ok.text
    assert _rev(ok) == revision + 1 and ok.headers["ETag"] == f'"{revision + 1}"' and "Warning" not in ok.headers

    # stale: 412 with the current resource exactly as GET returns it, and nothing is written
    stale = client.patch(url, json=body(2), headers={**h, "If-Match": f'"{revision}"'})
    assert stale.status_code == 412, stale.text
    detail = stale.json()
    assert detail["code"] == "revision_mismatch" and detail["current_revision"] == revision + 1
    assert isinstance(detail["detail"], str) and stale.headers["ETag"] == f'"{revision + 1}"'
    now = client.get(url, headers=h)
    assert detail["current"] == now.json() and _rev(now) == revision + 1

    # missing If-Match: still allowed (transitional) with a Warning, and bumps
    blind = client.patch(url, json=body(3), headers=h)
    assert blind.status_code == 200, blind.text
    assert blind.headers["Warning"] == WARNING_MISSING and _rev(blind) == revision + 2

    # `*` matches any revision; the bare integer and the weak form are accepted
    star = client.patch(url, json=body(4), headers={**h, "If-Match": "*"})
    assert star.status_code == 200 and _rev(star) == revision + 3 and "Warning" not in star.headers
    bare = client.patch(url, json=body(5), headers={**h, "If-Match": str(revision + 3)})
    assert bare.status_code == 200 and _rev(bare) == revision + 4
    weak = client.patch(url, json=body(6), headers={**h, "If-Match": f'W/"{revision + 4}"'})
    assert weak.status_code == 200 and _rev(weak) == revision + 5

    # EAP_REQUIRE_IF_MATCH=1: a missing header is 428, a sent one still works
    monkeypatch.setenv("EAP_REQUIRE_IF_MATCH", "1")
    required = client.patch(url, json=body(7), headers=h)
    assert required.status_code == 428 and required.json()["code"] == "precondition_required"
    assert _rev(client.get(url, headers=h)) == revision + 5  # nothing written
    allowed = client.patch(url, json=body(8), headers={**h, "If-Match": f'"{revision + 5}"'})
    assert allowed.status_code == 200 and _rev(allowed) == revision + 6


@pytest.mark.parametrize("kind", ["agent", "workflow", "schedule", "scorer", "mcp_server"])
def test_a_garbled_if_match_is_400_and_a_missing_resource_stays_404(world, kind):
    client, tenant_of = world
    h = tenant_of()
    url, body = RESOURCES[kind](client, h)
    bad = client.patch(url, json=body(1), headers={**h, "If-Match": "not-an-etag"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_if_match"
    missing = url.rsplit("/", 1)[0] + "/" + str(uuid.uuid4())
    gone = client.patch(missing, json=body(1), headers={**h, "If-Match": '"1"'})
    assert gone.status_code == 404


def test_lists_carry_revision_per_item_and_no_etag(world):
    client, tenant_of = world
    h = tenant_of()
    _agent_resource(client, h)
    listed = client.get(f"{API}/agents", headers=h)
    assert listed.status_code == 200 and "ETag" not in listed.headers
    assert all(isinstance(a["revision"], int) for a in listed.json())
    _workflow_resource(client, h)
    assert all("revision" in w for w in client.get(f"{API}/workflows", headers=h).json())
    _scorer_resource(client, h)
    assert all("revision" in s for s in client.get(f"{API}/scorers", headers=h).json()["items"])


def test_publish_restore_and_archive_bump_the_agent_and_answer_the_etag(world):
    client, tenant_of = world
    h = tenant_of()
    url, body = _agent_resource(client, h)
    r0 = _rev(client.get(url, headers=h))
    published = client.post(f"{url}/versions", headers=h)
    assert published.status_code == 201 and published.headers["ETag"] == f'"{r0 + 1}"'
    assert _rev(client.get(url, headers=h)) == r0 + 1
    # the editor that read before the publish is now stale: its save is refused, not silently merged
    stale = client.patch(url, json=body(1), headers={**h, "If-Match": f'"{r0}"'})
    assert stale.status_code == 412 and stale.json()["current"]["latest_version"] == 1
    archived = client.post(f"{url}/archive", headers=h)
    assert archived.status_code == 200 and archived.headers["ETag"] == f'"{r0 + 2}"' and _rev(archived) == r0 + 2


def test_concurrent_writers_with_the_same_etag_one_wins(world):
    """Two requests that both read revision R: the guarded UPDATE lets one through; the other is 412."""
    import concurrent.futures

    client, tenant_of = world
    h = tenant_of()
    url, body = _agent_resource(client, h)
    revision = _rev(client.get(url, headers=h))

    def write(n: int):
        return client.patch(url, json=body(n), headers={**h, "If-Match": f'"{revision}"'}).status_code

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(write, [1, 2]))
    assert codes == [200, 412]
    assert _rev(client.get(url, headers=h)) == revision + 1


def test_openapi_documents_the_header_and_the_etag_response(world):
    client, _ = world
    paths = client.get("/openapi.json").json()["paths"]
    for route in ("/api/v1/agents/{agent_id}", "/api/v1/workflows/{workflow_id}", "/api/v1/scorers/{scorer_id}",
                  "/api/v1/mcp-servers/{server_id}", "/api/v1/settings/connections/{connection_id}",
                  "/api/v1/eval-suites/{suite_id}", "/api/v1/eval-suites/{suite_id}/cases/{case_id}",
                  "/api/v1/workflows/schedules/{schedule_id}",
                  "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}"):
        patch = paths[route]["patch"]
        names = {p["name"] for p in patch.get("parameters", [])}
        assert "If-Match" in names, route
        assert "ETag" in patch["responses"]["200"]["headers"], route
        assert "412" in patch["responses"] and "428" in patch["responses"], route
        assert "ETag" in paths[route]["get"]["responses"]["200"]["headers"], route
