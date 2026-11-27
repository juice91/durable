"""IAM P2b wave 2: per-resource authorisation in the data-plane, observability, evaluation and admin services.

The same three engines as wave 1, against the real API on Postgres: one that DENIES everything, the real `OpenFgaPolicy`
over `ModelServer` (viewer, editor, admin, a grant, a non-member), and allow-all (today's behaviour). The guard that no
request path is left without an Access is `test_iam_access_guard.py`."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from eap_secrets import KEY_ENV, generate_key

from .iam_model_engine import AllowAll, DenyAll, Recording
from sqlalchemy import text

from .test_iam_p2b_wave1 import API, ROLES, World  # noqa: F401
from .test_iam_p2b_wave1 import world as _wave1_world
from .test_workflow_runs_api import publish_demo

SECRET = "fake-not-a-real-credential-0001"
UNKNOWN = str(uuid.uuid4())


@pytest.fixture
def world(_wave1_world):
    """Wave 1's world, with the rows this wave's tests leave behind removed afterwards."""
    yield _wave1_world
    engine = _wave1_world.app.state.database_engine
    with engine.begin() as conn:
        for table in ("kb_chunks", "kb_documents", "knowledge_bases", "tenant_secrets", "api_keys", "agent_scorers", "scorers",
                      "eval_case_results", "eval_cases", "eval_suites"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": _wave1_world.tenant})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    monkeypatch.setenv(KEY_ENV, generate_key())


def _kb(w: World, user: str, name: str = "Docs"):
    return w.client.post(f"{API}/knowledge-bases", json={"name": name, "embedding_model": "test:hash"}, headers=w.h(user))


def _doc(w: World, user: str, kb_id: str, key: str = "a.md"):
    return w.client.put(
        f"{API}/knowledge-bases/{kb_id}/documents/{key}", json={"content": "# A\n\nhello", "mime_type": "text/markdown"},
        headers=w.h(user),
    )


def _secret(w: World, user: str, name: str = "crm_token"):
    return w.client.put(f"{API}/secrets/{name}", json={"value": SECRET}, headers=w.h(user))


def _suite(w: World, user: str, agent_id: str, name: str = "smoke"):
    return w.client.post(
        f"{API}/agents/{agent_id}/eval-suites", json={"name": name, "evaluators": [{"kind": "equals_expected"}]}, headers=w.h(user)
    )


def _is_empty(body) -> bool:
    if isinstance(body, list):
        return body == []
    return isinstance(body, dict) and "items" in body and body["items"] == []


# --------------------------------------------------------------------------------- an engine that denies everything


def test_deny_all_refuses_every_protected_data_plane_call_and_changes_nothing(world: World):
    agent_id, _ = world.seed()
    kb_id = _kb(world, "ann").json()["id"]
    assert _secret(world, "ann").status_code == 200
    before = {t: world.count(t) for t in ("knowledge_bases", "kb_documents", "tenant_secrets", "mcp_servers")}
    world.use(DenyAll())
    c, h = world.client, world.h("ann")

    # Lists show nothing.
    assert _is_empty(c.get(f"{API}/knowledge-bases", headers=h).json())
    assert c.get(f"{API}/secrets", headers=h).json() == []
    assert c.get(f"{API}/mcp-servers", headers=h).json() == []

    kb = f"{API}/knowledge-bases/{kb_id}"
    refused = [
        ("get", kb, None), ("patch", kb, {"name": "x"}), ("delete", kb, None), ("get", f"{kb}/documents", None),
        ("get", f"{kb}/documents/a.md", None),
        ("put", f"{kb}/documents/a.md", {"content": "x", "mime_type": "text/markdown"}),
        ("delete", f"{kb}/documents/a.md", None), ("post", f"{kb}/documents/a.md/ingest", None),
        ("post", f"{kb}/reindex", {"embedding_model": "test:hash768"}), ("delete", f"{kb}/reindex", None),
        ("post", f"{kb}/search", {"query": "hi"}), ("get", f"{kb}/sources", None),
        ("post", f"{kb}/sources", {"name": "s", "connection_id": UNKNOWN, "kind": "x", "provider": "y", "config": {}}),
        ("get", f"{kb}/sources/{UNKNOWN}", None), ("post", f"{kb}/sources/{UNKNOWN}/sync", {"mode": "full"}),
        ("get", f"{API}/embedding-models", None),
        ("get", f"{API}/kb-sources/adapters", None), ("get", f"{API}/kb-sources/workflow-template", None),
        ("put", f"{API}/secrets/crm_token", {"value": SECRET}), ("put", f"{API}/secrets/brand_new", {"value": SECRET}),
        ("delete", f"{API}/secrets/crm_token", None),
        ("get", f"{API}/mcp-servers/{UNKNOWN}", None), ("patch", f"{API}/mcp-servers/{UNKNOWN}", {"enabled": False}),
        ("delete", f"{API}/mcp-servers/{UNKNOWN}", None), ("get", f"{API}/mcp-servers/{UNKNOWN}/runtime", None),
        ("post", f"{API}/mcp-servers/{UNKNOWN}/runtime/start", None), ("get", f"{API}/mcp-servers/{UNKNOWN}/runtime/logs", None),
        ("post", f"{API}/mcp-servers/{UNKNOWN}/test", None),
        ("post", f"{API}/mcp-servers", {"name": "s", "url": "https://example.com/mcp"}),
        ("post", f"{API}/mcp-servers/from-catalog", {"catalog_name": "x/y"}),
        ("get", f"{API}/mcp-catalog", None), ("get", f"{API}/mcp-catalog/x/y", None),
        ("get", f"{API}/settings/connections", None), ("get", f"{API}/settings/connections/{UNKNOWN}", None),
        ("post", f"{API}/settings/connections", {"name": "c", "kind": "api_token", "credential_refs": {}}),
        ("delete", f"{API}/settings/connections/{UNKNOWN}", None), ("post", f"{API}/settings/connections/{UNKNOWN}/check", None),
        ("get", f"{API}/tools", None), ("get", f"{API}/tools/clock.now", None),
        ("post", f"{API}/tools/clock.now/runs", {"input": {}}),
        ("get", f"{API}/processors", None), ("post", f"{API}/processors/test", {"processors": [{"type": "pii_redact"}], "text": "x"}),
                ("get", f"{API}/attachments/{UNKNOWN}", None),
        ("get", f"{API}/agents/{agent_id}/memory/working", None),
        ("get", f"{API}/conversations/{UNKNOWN}/memory/observations", None),
        ("delete", f"{API}/memory/resources/someone", None),
    ]
    wrong = []
    for method, url, body in refused:
        r = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
        if r.status_code not in (403, 404):
            wrong.append((method, url, r.status_code, r.text[:120]))
    assert not wrong, wrong
    assert c.post(f"{API}/attachments", files={"file": ("a.txt", b"hi", "text/plain")}, headers=h).status_code in (403, 404)
    assert c.post(f"{API}/knowledge-bases", json={"name": "No", "embedding_model": "test:hash"}, headers=h).status_code == 403

    world.use(world.engine)
    assert {t: world.count(t) for t in before} == before


def test_deny_all_refuses_observability_evaluation_and_admin_calls(world: World):
    agent_id, workflow_id = world.seed()
    suite_id = _suite(world, "ann", agent_id).json()["id"]
    world.use(DenyAll())
    c, h = world.client, world.h("ann")
    assert c.get(f"{API}/eval-suites", headers=h).json()["items"] == []
    assert c.get(f"{API}/evals/runs", headers=h).json()["items"] == []
    assert c.get(f"{API}/spans", headers=h).json()["items"] == []
    assert c.get(f"{API}/scorers", headers=h).status_code == 404
    assert c.get(f"{API}/api-keys", headers=h).json()["items"] == []
    assert c.get(f"{API}/evals/scorers", headers=h).json()["items"] == []
    assert c.get(f"{API}/processor-results", headers=h).json()["items"] == []

    refused = [
        ("get", f"{API}/agents/{agent_id}/eval-suites", None), ("post", f"{API}/agents/{agent_id}/eval-suites", {"name": "n", "evaluators": []}),
        ("get", f"{API}/workflows/{workflow_id}/evals/suites", None),
        ("get", f"{API}/eval-suites/{suite_id}", None), ("patch", f"{API}/eval-suites/{suite_id}", {"name": "z"}),
        ("delete", f"{API}/eval-suites/{suite_id}", None), ("get", f"{API}/eval-suites/{suite_id}/cases", None),
        ("post", f"{API}/eval-suites/{suite_id}/cases", {"name": "c", "input": "hi"}),
        ("get", f"{API}/eval-suites/{suite_id}/export", None),
        ("post", f"{API}/agents/{agent_id}/evals/runs", {"run_id": str(uuid.uuid4()), "suite_id": suite_id}),
        ("get", f"{API}/agents/{agent_id}/evals/runs", None), ("get", f"{API}/agents/{agent_id}/evals/warnings", None),
        ("post", f"{API}/scorers", {"name": "s", "evaluator": {"kind": "equals_expected"}}),
        ("get", f"{API}/scorers/{UNKNOWN}", None), ("delete", f"{API}/scorers/{UNKNOWN}", None),
        ("get", f"{API}/agents/{agent_id}/scorers", None), ("put", f"{API}/agents/{agent_id}/scorers/{UNKNOWN}", {"sampling_rate": "1"}),
        ("post", f"{API}/agents/{agent_id}/scorer-suggestions", {}), ("post", f"{API}/workflows/{workflow_id}/scorer-suggestions", {}),
        ("get", f"{API}/runs/{UNKNOWN}/trace", None), ("get", f"{API}/traces", None), ("get", f"{API}/traces/{uuid.uuid4().hex}", None),
        ("post", f"{API}/traces/{UNKNOWN}/feedback", {"rating": "up"}), ("get", f"{API}/traces/{UNKNOWN}/feedback", None),
        ("get", f"{API}/logs", None), ("get", f"{API}/logs?agent_id={agent_id}", None),
        ("get", f"{API}/metrics/summary", None), ("get", f"{API}/metrics/timeseries", None),
        ("get", f"{API}/metrics/summary?agent_id={agent_id}", None), ("get", f"{API}/metrics/breakdown?by=agent", None),
        ("get", f"{API}/spans?agent_id={agent_id}", None), ("get", f"{API}/runs/histogram?agent_id={agent_id}", None),
        ("get", f"{API}/workflows/{workflow_id}/explanation", None), ("post", f"{API}/workflows/{workflow_id}/explanation", {}),
        ("post", f"{API}/api-keys", {"name": "k", "scopes": ["mcp"]}), ("delete", f"{API}/api-keys/{UNKNOWN}", None),
        ("get", f"{API}/architect", None), ("get", f"{API}/architect/options", None), ("post", f"{API}/architect", None),
        ("put", f"{API}/architect/model", {"model": "test:echo"}), ("post", f"{API}/architect/draft", {"description": "x"}),
        ("post", f"{API}/architect/draft-workflow", {"description": "x"}),
        ("post", f"{API}/architect/agents", {"spec": {"name": "N", "instructions": "i", "model": "test:echo"}}),
    ]
    wrong = []
    for method, url, body in refused:
        r = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
        if r.status_code not in (403, 404):
            wrong.append((method, url, r.status_code, r.text[:120]))
    assert not wrong, wrong


def test_a_failing_engine_fails_closed_with_503_in_the_new_domains(world: World):
    world.seed()
    import httpx2

    def broken(request):
        raise httpx2.ConnectError("down")

    world.engine.engine.client._http = httpx2.AsyncClient(transport=httpx2.MockTransport(broken))
    c, h = world.client, world.h("ann")
    for r in (
        c.get(f"{API}/knowledge-bases", headers=h), _kb(world, "ann", "Late"), c.get(f"{API}/secrets", headers=h),
        _secret(world, "ann"), c.get(f"{API}/mcp-servers", headers=h), c.get(f"{API}/spans", headers=h),
        c.get(f"{API}/eval-suites", headers=h), c.get(f"{API}/metrics/summary", headers=h), c.get(f"{API}/tools", headers=h),
        c.get(f"{API}/api-keys", headers=h),
    ):
        assert r.status_code == 503, (r.request.url, r.status_code, r.text)


# --------------------------------------------------------------------------------------- allow-all is today


def test_an_allow_all_engine_reproduces_todays_behaviour(world: World):
    world.use(AllowAll())
    c, h = world.client, world.h("anyone")
    kb = _kb(world, "anyone")
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]
    assert _doc(world, "anyone", kb_id).status_code in (200, 202)
    assert [k["id"] for k in c.get(f"{API}/knowledge-bases", headers=h).json()["items"]] == [kb_id]
    assert c.delete(f"{API}/knowledge-bases/{kb_id}?force=true", headers=h).status_code == 204
    assert _secret(world, "anyone").status_code == 200
    assert [s["name"] for s in c.get(f"{API}/secrets", headers=h).json()] == ["crm_token"]
    assert c.delete(f"{API}/secrets/crm_token", headers=h).status_code == 204
    assert c.get(f"{API}/tools", headers=h).status_code == 200
    assert c.get(f"{API}/metrics/summary", headers=h).status_code == 200
    created = c.post(f"{API}/api-keys", json={"name": "k", "scopes": ["mcp"]}, headers=h)
    assert created.status_code == 201
    assert c.delete(f"{API}/api-keys/{created.json()['id']}", headers=h).status_code == 204


# ------------------------------------------------------------------------------ the real model: knowledge bases


def test_a_viewer_reads_a_knowledge_base_an_editor_edits_it_and_only_an_owner_or_admin_deletes_it(world: World):
    world.use(world.engine)
    made = _kb(world, "ann")
    assert made.status_code == 201
    kb_id = made.json()["id"]
    assert ("user:ann", "owner", f"knowledge_base:{world.tenant}/{kb_id}") in world.server.tuples  # before the response
    assert _doc(world, "ann", kb_id).status_code in (200, 202)
    kb = f"{API}/knowledge-bases/{kb_id}"

    vic, eve, zed = world.h("vic"), world.h("eve"), world.h("zed")
    c = world.client
    assert [k["id"] for k in c.get(f"{API}/knowledge-bases", headers=vic).json()["items"]] == [kb_id]
    assert c.get(kb, headers=vic).status_code == 200
    assert c.get(f"{kb}/documents", headers=vic).status_code == 200
    assert c.get(f"{kb}/documents/a.md", headers=vic).status_code == 200
    assert c.get(f"{kb}/sources", headers=vic).status_code == 200
    # A viewer cannot edit, search (a run of the knowledge base) or create one.
    assert _doc(world, "vic", kb_id, "b.md").status_code == 403
    assert c.patch(kb, json={"name": "Renamed"}, headers=vic).status_code == 403
    assert c.delete(f"{kb}/documents/a.md", headers=vic).status_code == 403
    assert c.post(f"{kb}/search", json={"query": "hello"}, headers=vic).status_code == 403
    assert _kb(world, "vic", "Mine").status_code == 403
    # An editor edits and searches what it does not own, but archives (deletes) only its own.
    assert _doc(world, "eve", kb_id, "b.md").status_code in (200, 202)
    assert c.patch(kb, json={"name": "Renamed"}, headers=eve).status_code == 200
    assert c.delete(kb, headers=eve).status_code == 403
    mine = _kb(world, "eve", "Eve's").json()["id"]
    assert c.delete(f"{API}/knowledge-bases/{mine}", headers=eve).status_code == 204
    assert not [t for t in world.server.tuples if t[2] == f"knowledge_base:{world.tenant}/{mine}"]  # unregistered on delete
    # Not a member: it cannot tell the base exists.
    assert c.get(f"{API}/knowledge-bases", headers=zed).json()["items"] == []
    assert c.get(kb, headers=zed).status_code == 404
    assert c.get(f"{kb}/documents", headers=zed).status_code == 404
    # A runner grant lets a viewer search that base only.
    world.grant("vic", "runner", "knowledge_base", kb_id)
    assert c.post(f"{kb}/search", json={"query": "hello"}, headers=vic).status_code != 403
    assert c.delete(kb, headers=world.h("ann")).status_code == 204


def test_secrets_are_listed_to_members_written_by_admins_and_deleted_by_owner_or_admin(world: World):
    world.use(world.engine)
    c = world.client
    assert _secret(world, "eve").status_code == 403  # creating a secret is the admin's (design 2.2)
    assert _secret(world, "ann").status_code == 200
    assert ("user:ann", "owner", f"secret:{world.tenant}/crm_token") in world.server.tuples
    assert [s["name"] for s in c.get(f"{API}/secrets", headers=world.h("vic")).json()] == ["crm_token"]  # metadata for everyone
    assert c.get(f"{API}/secrets", headers=world.h("zed")).json() == []
    assert _secret(world, "vic").status_code == 403 and _secret(world, "eve").status_code == 403  # replace: edit
    assert c.delete(f"{API}/secrets/crm_token", headers=world.h("eve")).status_code == 403
    assert c.delete(f"{API}/secrets/crm_token", headers=world.h("zed")).status_code == 404
    assert _secret(world, "ann").status_code == 200  # an admin replaces
    assert c.delete(f"{API}/secrets/crm_token", headers=world.h("ann")).status_code == 204
    assert not [t for t in world.server.tuples if t[2] == f"secret:{world.tenant}/crm_token"]


def test_a_legacy_usable_by_tuple_grants_nothing_any_more(world: World):
    """Agent-bound grants: only an admin grants use of a secret, and the old `usable_by` relation (now `legacy_usable_by`) is
    read by nothing, so a person with such a tuple still may not attach the secret."""
    from eap_iam import Principal
    from eap_iam_openfga.client import TupleKey

    from eap_api.services.authz import AuthzService
    from eap_iam import ResourceRef

    world.use(world.engine)
    assert _secret(world, "ann").status_code == 200
    assert _secret(world, "ann", "other_token").status_code == 200
    authz = AuthzService(world.engine)
    vic = Principal(tenant_id=world.tenant, kind="user", subject="vic", provider="t")

    def may(name: str) -> bool:
        return asyncio.run(authz.check(vic, "grant_use", ResourceRef("secret", name, world.tenant))).allowed

    assert not may("crm_token")
    asyncio.run(world.engine.engine.client.write([TupleKey("user:vic", "legacy_usable_by", f"secret:{world.tenant}/crm_token")]))
    assert not may("crm_token") and not may("other_token")


# ---------------------------------------------------------------------------- the real model: evals and traces


def test_an_eval_follows_its_target_viewers_read_editors_edit_and_run(world: World):
    agent_id, _ = world.seed()
    c = world.client
    assert _suite(world, "vic", agent_id).status_code == 403
    suite = _suite(world, "eve", agent_id)
    assert suite.status_code == 201, suite.text
    suite_id = suite.json()["id"]
    url = f"{API}/eval-suites/{suite_id}"
    assert c.get(url, headers=world.h("vic")).status_code == 200
    assert c.get(f"{API}/agents/{agent_id}/eval-suites", headers=world.h("vic")).status_code == 200
    assert [s["suite"]["id"] if "suite" in s else s["id"] for s in c.get(f"{API}/eval-suites", headers=world.h("vic")).json()["items"]] == [suite_id]
    assert c.patch(url, json={"name": "Renamed"}, headers=world.h("vic")).status_code == 403
    assert c.post(f"{url}/cases", json={"name": "c1", "input": "hi"}, headers=world.h("vic")).status_code == 403
    assert c.post(f"{url}/cases", json={"name": "c1", "input": "hi"}, headers=world.h("eve")).status_code == 201
    run = {"run_id": str(uuid.uuid4()), "suite_id": suite_id}
    assert c.post(f"{API}/agents/{agent_id}/evals/runs", json=run, headers=world.h("vic")).status_code == 403  # an eval is a run
    assert c.post(f"{API}/agents/{agent_id}/evals/runs", json=run, headers=world.h("eve")).status_code in (200, 201, 202)
    assert c.get(f"{API}/agents/{agent_id}/evals/runs", headers=world.h("vic")).status_code == 200
    # A non-member sees neither the suite nor the overview rows.
    assert c.get(url, headers=world.h("zed")).status_code == 404
    assert c.get(f"{API}/eval-suites", headers=world.h("zed")).json()["items"] == []
    assert c.get(f"{API}/evals/runs", headers=world.h("zed")).json()["items"] == []
    # Scorers: the library is tenant configuration (read by members, written by editors), attaching is `edit` on the agent.
    scorer = c.post(f"{API}/scorers", json={"name": "exact", "evaluator": {"kind": "regex", "pattern": "^h"}}, headers=world.h("eve"))
    assert scorer.status_code == 201, scorer.text
    assert c.post(f"{API}/scorers", json={"name": "other", "evaluator": {"kind": "regex", "pattern": "^h"}}, headers=world.h("vic")).status_code == 403
    assert c.get(f"{API}/scorers", headers=world.h("vic")).status_code == 200
    assert c.get(f"{API}/scorers", headers=world.h("zed")).status_code == 404
    attach = f"{API}/agents/{agent_id}/scorers/{scorer.json()['id']}"
    assert c.put(attach, json={"sampling_rate": "1"}, headers=world.h("vic")).status_code == 403


def test_a_trace_is_as_readable_as_the_workflow_it_belongs_to(world: World):
    _, workflow_id = world.seed()
    other = publish_demo(world.client, world.h("ann"))
    c = world.client
    mine = c.post(f"{API}/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}}, headers=world.h("ann")).json()["run_id"]
    theirs = c.post(f"{API}/workflows/{other}/runs", json={"input": {"q": "hi"}}, headers=world.h("ann")).json()["run_id"]
    assert c.get(f"{API}/runs/{mine}/trace", headers=world.h("vic")).status_code == 200  # a viewer reads traces
    assert c.get(f"{API}/runs/{mine}/trace", headers=world.h("zed")).status_code == 404
    assert c.post(f"{API}/traces/{mine}/feedback", json={"rating": "up"}, headers=world.h("zed")).status_code == 404
    world.grant("zed", "runner", "workflow", workflow_id)
    assert c.get(f"{API}/runs/{mine}/trace", headers=world.h("zed")).status_code == 200
    assert c.get(f"{API}/runs/{theirs}/trace", headers=world.h("zed")).status_code == 404  # no grant on this workflow
    assert c.get(f"{API}/spans", headers=world.h("zed")).status_code == 200
    spans = c.get(f"{API}/spans", headers=world.h("zed")).json()["items"]
    assert theirs not in {s["run_id"] for s in spans}


def test_tenant_wide_metrics_and_logs_are_the_admins_and_one_agents_are_a_reads(world: World):
    agent_id, _ = world.seed()
    c = world.client
    assert c.get(f"{API}/metrics/summary", headers=world.h("ann")).status_code == 200
    assert c.get(f"{API}/metrics/summary", headers=world.h("vic")).status_code == 403  # the tenant-wide total spans every agent
    assert c.get(f"{API}/metrics/summary", headers=world.h("zed")).status_code == 404
    assert c.get(f"{API}/metrics/summary?agent_id={agent_id}", headers=world.h("vic")).status_code == 200
    assert c.get(f"{API}/metrics/summary?agent_id={agent_id}", headers=world.h("zed")).status_code == 404
    assert c.get(f"{API}/metrics/breakdown?by=agent", headers=world.h("eve")).status_code == 403


def test_api_keys_are_the_admins_and_a_member_without_a_role_cannot_hold_a_personal_one(world: World):
    world.use(world.engine)
    c = world.client
    assert c.post(f"{API}/api-keys", json={"name": "k", "scopes": ["mcp"]}, headers=world.h("eve")).status_code == 403
    made = c.post(f"{API}/api-keys", json={"name": "k", "scopes": ["mcp"]}, headers=world.h("ann"))
    assert made.status_code == 201, made.text
    key_id = made.json()["id"]
    assert ("user:ann", "owner", f"api_key:{world.tenant}/{key_id}") in world.server.tuples
    assert [k["id"] for k in c.get(f"{API}/api-keys", headers=world.h("ann")).json()["items"]] == [key_id]
    assert c.get(f"{API}/api-keys", headers=world.h("eve")).json()["items"] == []  # not the owner, not an admin
    assert c.delete(f"{API}/api-keys/{key_id}", headers=world.h("eve")).status_code == 404
    assert c.delete(f"{API}/api-keys/{key_id}", headers=world.h("ann")).status_code == 204


def test_an_architect_turn_follows_the_person_it_acts_for(world: World):
    world.use(world.engine)
    c = world.client
    assert c.get(f"{API}/architect", headers=world.h("vic")).status_code == 200  # a viewer reads the state
    assert c.get(f"{API}/architect", headers=world.h("zed")).status_code == 404
    assert c.post(f"{API}/architect", headers=world.h("vic")).status_code == 403  # seeding creates agents: editors
    assert c.post(f"{API}/architect/draft", json={"description": "x"}, headers=world.h("vic")).status_code == 403
    assert c.post(f"{API}/architect/draft-workflow", json={"description": "x"}, headers=world.h("vic")).status_code == 403
    assert c.put(f"{API}/architect/model", json={"model": "test:echo"}, headers=world.h("eve")).status_code == 403  # a tenant setting
    spec = {"name": "Helper", "instructions": "Help.", "model": "test:echo"}
    assert c.post(f"{API}/architect/agents", json={"spec": spec}, headers=world.h("vic")).status_code == 403


# ------------------------------------------------------------------------------------------------- engine cost


def test_the_new_lists_cost_a_constant_number_of_engine_calls(world: World):
    world.use(world.engine)
    assert _secret(world, "ann").status_code == 200
    assert _kb(world, "ann").status_code == 201
    recording = Recording(world.engine)
    world.use(recording)
    h = world.h("vic")
    paths = [f"{API}/knowledge-bases", f"{API}/secrets", f"{API}/mcp-servers", f"{API}/eval-suites", f"{API}/evals/runs",
             f"{API}/evals/scorers", f"{API}/spans", f"{API}/api-keys", f"{API}/processor-results"]

    def calls_for(path: str) -> tuple[int, int]:
        recording.calls.clear()
        before = sum(world.server.calls.values())
        assert world.client.get(path, headers=h).status_code in (200, 403, 404), path
        return sum(recording.calls.values()), sum(world.server.calls.values()) - before

    one = {p: calls_for(p) for p in paths}
    for i in range(10):
        assert _kb(world, "ann", f"Extra {i}").status_code == 201
        assert _secret(world, "ann", f"extra_{i}").status_code == 200
        world.client.post(f"{API}/agents", json={"spec": {"name": f"A{i}", "instructions": "x", "model": {"provider": "test", "name": "echo"}}}, headers=world.h("ann"))
    many = {p: calls_for(p) for p in paths}
    assert many == one  # eleven of each cost exactly what one did
    assert one[f"{API}/knowledge-bases"][0] == 1 and one[f"{API}/secrets"][0] == 1


# ------------------------------------------------------------------------------------------- AG-UI resume


def test_an_agui_resume_is_an_approval_and_needs_the_approve_right(world: World):
    agent_id, workflow_id = world.seed()
    interrupt = f"approval:{uuid.uuid4()}:tool:call_1"
    body = {"threadId": "t1", "runId": "r1", "messages": [], "tools": [], "context": [], "state": {}, "forwardedProps": {},
            "resume": [{"interruptId": interrupt, "status": "resolved", "payload": {"approved": True}}]}
    c = world.client
    # An editor may run the agent but not decide approvals: refused before anything is read or written.
    r = c.post(f"{API}/ag-ui/agents/{agent_id}", json=body, headers=world.h("eve"))
    assert r.status_code == 403, r.text
    w = c.post(f"{API}/ag-ui/workflows/{workflow_id}", json=body, headers=world.h("eve"))
    assert w.status_code == 403, w.text
    # An admin passes the check (and is then told the thread has nothing to resume: a different refusal).
    assert c.post(f"{API}/ag-ui/agents/{agent_id}", json=body, headers=world.h("ann")).status_code not in (403,)
    # A viewer cannot even run it: 403 as for any run, the same refusal before the resume is read.
    assert c.post(f"{API}/ag-ui/agents/{agent_id}", json=body, headers=world.h("vic")).status_code == 403
    assert world.count("runs") == 0


def test_a_personal_key_needs_membership_in_the_tenant(world: World):
    """Person-only keys: the engine must let the caller read the tenant (a non-member holds no key in it)."""
    from eap_iam import Principal

    from eap_api.services.authz import AuthzService
    from eap_api.services.errors import Forbidden
    from eap_api.services.personal_api_keys import PersonalApiKeyService

    class Store:
        def list_owned(self, tenant, subject):
            return []

    def person(subject: str) -> Principal:
        return Principal(tenant_id=world.tenant, kind="user", subject=subject, provider="zitadel")

    service = PersonalApiKeyService(Store(), None, AuthzService(world.engine))
    assert asyncio.run(service.list(person("vic"))) == []  # a viewer is a member
    with pytest.raises(Forbidden) as refused:
        asyncio.run(service.list(person("zed")))
    assert refused.value.code == "not_a_member"
    with pytest.raises(Forbidden):
        asyncio.run(PersonalApiKeyService(Store(), None, AuthzService(DenyAll())).list(person("ann")))
