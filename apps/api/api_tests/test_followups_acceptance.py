"""Acceptance tests for the 2026-11-15 follow-up batch (items 2, 3, 4, 5 and 7): cross-tenant
behaviour, exact statuses and messages, query counts, byte-identical pricing and the archive matrix.
The implementer's own tests live in test_agent_archive, test_logs_agent_filter, test_runs_chat_usage,
test_memory_missing_note and packages/pricing."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event

import eap_pricing
from eap_api.agent_archive import AGENT_ARCHIVED
from eap_api.routes.knowledge import estimate_embedding_cost

from .conftest import create_agent, get_auth_header, publish_version
from .run_helpers import requires_pg
from .test_conversations import create_conversation, new_conversation, turn
from .test_logs import _entry, fake_loki  # noqa: F401 - fixture
from .test_memory_api import API, conv_wm, make_agent as make_mem_agent, mem_tenant, res_wm  # noqa: F401
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    start,
    stream_settings,
    tenant_of,
)

REPO = Path(__file__).resolve().parents[3]


def _archive(client, h, agent_id, action="archive"):
    return client.post(f"/api/v1/agents/{agent_id}/{action}", headers=h)


def _agents(client, h, **params):
    r = client.get("/api/v1/agents", headers=h, params=params)
    assert r.status_code == 200, r.text
    return {a["id"]: a for a in r.json()}


# ---------------------------------------------------------------------------- 7. archive


@requires_pg
def test_archive_never_leaks_across_tenants_and_leaves_the_owner_untouched(pg, tenant_of):
    client, _ = pg
    _, mine = tenant_of()
    _, other = tenant_of()
    agent_id = make_agent(client, mine)
    conv_id = new_conversation(client, mine, agent_id)
    assert _archive(client, mine, agent_id).status_code == 200
    # The other tenant sees plain 404s (never the 409 that would reveal the agent exists).
    assert start(client, other, agent_id).status_code == 404
    assert create_conversation(client, other, agent_id).status_code == 404
    assert turn(client, other, conv_id).status_code == 404
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=other).status_code == 404
    assert _archive(client, other, agent_id, "unarchive").status_code == 404
    assert _agents(client, other, archived="true") == {} and _agents(client, other) == {}
    assert _agents(client, mine, archived="true")[agent_id]["archived"] is True  # still archived


@requires_pg
def test_archiving_one_agent_does_not_touch_its_siblings(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    a, b = make_agent(client, h), make_agent(client, h)
    _archive(client, h, a)
    assert start(client, h, b).status_code == 201
    assert create_conversation(client, h, b).status_code == 201
    assert _agents(client, h, archived="false").keys() == {b}
    assert _agents(client, h, archived="true").keys() == {a}


@requires_pg
def test_everything_already_recorded_stays_readable_and_the_draft_editable(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id=run_id).status_code == 201
    conv_id = new_conversation(client, h, agent_id)
    turn_id = str(uuid.uuid4())
    assert turn(client, h, conv_id, run_id=turn_id).status_code == 201
    assert _archive(client, h, agent_id).status_code == 200
    for path in (
        f"/agents/{agent_id}", f"/agents/{agent_id}/versions", f"/agents/{agent_id}/versions/1",
        f"/runs/{run_id}", f"/runs/{run_id}/events", f"/runs/{run_id}/trace", f"/conversations/{conv_id}",
        "/runs", "/conversations", f"/agents/{agent_id}/memory/working",
    ):
        r = client.get(f"/api/v1{path}", headers=h, params={"agent_id": agent_id} if path in ("/runs", "/conversations") else None)
        assert r.status_code == 200, (path, r.status_code, r.text)
    assert client.get(f"/api/v1/conversations/{conv_id}", headers=h).json()["runs"][0]["run_id"] == turn_id
    # The draft stays editable.
    patched = client.patch(
        f"/api/v1/agents/{agent_id}", headers=h,
        json={"spec": {"name": "Edited while archived", "model": {"provider": "test", "name": "ticket"}}},
    )
    assert patched.status_code == 200 and patched.json()["archived"] is True
    # Retries of recorded work answer instead of 409.
    assert start(client, h, agent_id, run_id=run_id).status_code == 200
    assert turn(client, h, conv_id, run_id=turn_id).status_code == 200
    assert create_conversation(client, h, agent_id, conversation_id=conv_id).status_code == 200


@requires_pg
def test_each_refusal_has_the_exact_status_message_and_creates_nothing(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    conv_id = new_conversation(client, h, agent_id)
    _archive(client, h, agent_id)
    fresh_run, fresh_turn, fresh_conv = (str(uuid.uuid4()) for _ in range(3))
    refusals = [
        start(client, h, agent_id, run_id=fresh_run),
        start(client, h, agent_id, run_id=fresh_run, version=1),
        start(client, h, agent_id, run_id=fresh_run, version="draft"),
        create_conversation(client, h, agent_id, conversation_id=fresh_conv),
        turn(client, h, conv_id, run_id=fresh_turn),
        turn(client, h, conv_id, run_id=fresh_turn, version="draft"),
        client.post(f"/api/v1/agents/{agent_id}/versions", headers=h),
        client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", json={"discard_draft": False}, headers=h),
    ]
    for r in refusals:
        assert (r.status_code, r.json()) == (409, {"detail": AGENT_ARCHIVED}), r.text
    assert "unarchive" in AGENT_ARCHIVED.lower()
    for rid in (fresh_run, fresh_turn):
        assert client.get(f"/api/v1/runs/{rid}", headers=h).status_code == 404
    assert client.get(f"/api/v1/conversations/{fresh_conv}", headers=h).status_code == 404
    assert len(client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()) == 1  # nothing published


# ---------------------------------------------------------------------------- 2. GET /logs?agent_id=

NOW_NS = int(datetime.now(UTC).timestamp() * 1_000_000_000)


def _runs(client, tenant, n=1, agent_id=None):
    if agent_id is None:
        agent_id = create_agent(client, tenant, "alice")
        publish_version(client, agent_id, tenant, "alice")
    store, _ = client.app.state.run_stores
    ids = []
    for _ in range(n):
        rid = uuid.uuid4()
        store.create_run(tenant, uuid.UUID(agent_id), 1, "hi", rid)
        ids.append(str(rid))
    return agent_id, ids


def _logs(client, tenant, **params):
    return client.get("/api/v1/logs", params=params, headers=get_auth_header(tenant, "alice"))


def test_logs_without_agent_id_carry_no_agent_fields(client, tenant, fake_loki):
    _runs(client, tenant)
    body = _logs(client, tenant).json()
    assert "agent_runs" not in body and "agent_runs_limited" not in body


def test_workflow_child_runs_and_archived_agents_are_searched(client, tenant, fake_loki):
    from sqlalchemy import update
    from eap_run_store.store import RunModel

    agent, (parent_like, child) = _runs(client, tenant, 2)
    store, _ = client.app.state.run_stores
    with store.Session() as s, s.begin():
        s.execute(update(RunModel).where(RunModel.id == child).values(parent_run_id=parent_like, parent_step_id="s1"))
    assert client.post(f"/api/v1/agents/{agent}/archive", headers=get_auth_header(tenant, "alice")).status_code == 200
    fake_loki.entries = [_entry(NOW_NS - 1, "from child", tenant, child)]
    body = _logs(client, tenant, agent_id=agent).json()
    assert [i["message"] for i in body["items"]] == ["from child"]
    assert body["agent_runs"] == 2 and child in fake_loki.calls[0]["query"]


def test_the_cap_keeps_the_newest_runs_and_is_200(client, tenant, fake_loki, monkeypatch):
    from sqlalchemy import update
    from datetime import timedelta
    from eap_api.services import logs as logs_route
    from eap_run_store.store import RunModel

    assert logs_route.MAX_AGENT_RUNS == 200
    monkeypatch.setattr(logs_route, "MAX_AGENT_RUNS", 2)
    agent, ids = _runs(client, tenant, 3)
    store, _ = client.app.state.run_stores
    now = datetime.now(UTC)
    with store.Session() as s, s.begin():
        for age, rid in enumerate(ids):  # ids[0] newest ... ids[2] oldest
            s.execute(update(RunModel).where(RunModel.id == rid).values(created_at=now - timedelta(minutes=age)))
    body = _logs(client, tenant, agent_id=agent).json()
    query = fake_loki.calls[0]["query"]
    assert (body["agent_runs"], body["agent_runs_limited"]) == (2, True)
    assert ids[0] in query and ids[1] in query and ids[2] not in query


def test_agent_filter_composes_with_level_and_q_and_never_leaks_other_tenants(client, tenant, fake_loki):
    agent, (run,) = _runs(client, tenant)
    other_tenant = f"tb{uuid.uuid4().hex[:8]}"
    other_agent, (other_run,) = _runs(client, other_tenant)
    fake_loki.entries = [_entry(NOW_NS - 1, "boom here", tenant, run), _entry(NOW_NS - 2, "boom", other_tenant, run)]
    body = _logs(client, tenant, agent_id=agent, q="boom", level="info").json()
    assert [i["message"] for i in body["items"]] == ["boom here"]
    q = fake_loki.calls[0]["query"]
    assert f'eap_tenant_id="{tenant}"' in q and '|= "boom"' in q and "severity_number" in q
    # Another tenant's agent id or run id: 404, and Loki is never asked.
    n = len(fake_loki.calls)
    assert _logs(client, tenant, agent_id=other_agent).status_code == 404
    r = _logs(client, tenant, agent_id=agent, run_id=other_run)
    assert r.status_code == 404 and len(fake_loki.calls) == n
    assert _logs(client, tenant, agent_id="").status_code == 422


# ---------------------------------------------------------------------------- 3. chat usage

from .test_runs_chat_usage import MIXED, _row, _run_with_rows  # noqa: E402

CHAT = ("chat_input_tokens", "chat_output_tokens", "chat_cost_usd")


def _statements(engine, call):
    seen: list[str] = []

    def before(conn, cursor, statement, *a):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return seen


def test_runs_list_chat_usage_costs_no_extra_queries_per_run(client, tenant):
    agent_id, _ = _run_with_rows(client, tenant, MIXED)
    h = get_auth_header(tenant, "alice")
    engine = client.app.state.database_engine

    def listing():
        r = client.get("/api/v1/runs", headers=h, params={"agent_id": agent_id})
        assert r.status_code == 200
        return r.json()["items"]

    few = _statements(engine, listing)
    store, log = client.app.state.run_stores
    for i in range(7):
        rid = uuid.uuid4()
        store.create_run(tenant, uuid.UUID(agent_id), 1, "hi", rid)
        log.append(tenant, rid, "model:0:final", "model_message", {"model_request": 0}, _row("model:0", 1, 1, "0.1"))
    many = _statements(engine, listing)
    assert len(listing()) == 8
    assert len(many) == len(few), (len(few), len(many))
    assert sum("run_model_requests" in s for s in many) == 1
    assert sum("run_model_requests" in s for s in _statements(engine, lambda: client.get(f"/api/v1/runs/{uuid.uuid4()}", headers=h))) == 0


def test_chat_sums_equal_the_trace_split_on_every_surface(pg, tenant_of):
    client, _app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    conv = new_conversation(client, h, agent_id)
    run_id = str(uuid.uuid4())
    assert turn(client, h, conv, run_id=run_id).status_code == 201
    store, log = client.app.state.run_stores
    with client.app.state.database_engine.connect() as c:
        from sqlalchemy import text
        tenant = c.execute(text("SELECT tenant_id FROM runs WHERE id = :r"), {"r": run_id}).scalar_one()
    for i, row in enumerate(MIXED):
        log.append(tenant, uuid.UUID(run_id), f"{row['request_key']}:final", "model_message", {"model_request": i}, row)
    trace = client.get(f"/api/v1/runs/{run_id}/trace", headers=h).json()
    expected = tuple(trace["totals"][k] for k in CHAT)
    assert expected == (15, 5, "0.0012000000")
    surfaces = {
        "run": client.get(f"/api/v1/runs/{run_id}", headers=h).json(),
        "trace run": trace["run"],
        "list": next(r for r in client.get("/api/v1/runs", headers=h).json()["items"] if r["run_id"] == run_id),
        "conversation": next(r for r in client.get(f"/api/v1/conversations/{conv}", headers=h).json()["runs"] if r["run_id"] == run_id),
    }
    for name, run in surfaces.items():
        assert tuple(run[k] for k in CHAT) == expected, name
    # The totals (all operations) stay different from the chat split.
    assert surfaces["run"]["input_tokens"] != surfaces["run"]["chat_input_tokens"]


def test_other_tenants_cannot_read_chat_usage_and_see_no_foreign_runs(client, tenant):
    agent_id, run_id = _run_with_rows(client, tenant, MIXED)
    other = f"tb{uuid.uuid4().hex[:8]}"
    h = get_auth_header(other, "bob")
    assert client.get(f"/api/v1/runs/{run_id}", headers=h).status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}/trace", headers=h).status_code == 404
    assert client.get("/api/v1/runs", headers=h).json()["items"] == []
    assert client.get("/api/v1/runs", headers=h, params={"agent_id": agent_id}).json()["items"] == []


def test_draft_run_trace_marks_the_run_span_and_published_runs_do_not(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    draft, published = str(uuid.uuid4()), str(uuid.uuid4())
    assert start(client, h, agent_id, run_id=draft, version="draft").status_code == 201
    assert start(client, h, agent_id, run_id=published).status_code == 201
    spans = lambda rid: next(s for s in client.get(f"/api/v1/runs/{rid}/trace", headers=h).json()["spans"] if s["id"].startswith("run:"))
    assert spans(draft)["attributes"]["draft"] is True and spans(published)["attributes"]["draft"] is False


# ---------------------------------------------------------------------------- 4. ?missing=null


@requires_pg
def test_missing_null_has_the_same_404_body_for_unknown_and_foreign_owners(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    _, other = mem_tenant()
    aid = make_mem_agent(client, h, {"working": {"scope": "conversation"}})
    cid = new_conversation(client, h, aid)
    for url in (conv_wm(cid), conv_wm(str(uuid.uuid4())), res_wm(aid, "alice"), res_wm(str(uuid.uuid4()), "alice")):
        plain = client.get(url, headers=other)
        probed = client.get(url, params={"missing": "null"}, headers=other)
        assert (plain.status_code, plain.json()) == (probed.status_code, probed.json()) == (404, plain.json()), url
    for bad in ("", "NULL", "none", "0"):
        assert client.get(conv_wm(cid), params={"missing": bad}, headers=h).status_code == 422, bad
    # An existing note is identical with and without the probe (body and ETag).
    assert client.put(conv_wm(cid), json={"content": "n"}, headers={**h, "If-Match": '"0"'}).status_code == 200
    a, b = client.get(conv_wm(cid), headers=h), client.get(conv_wm(cid), params={"missing": "null"}, headers=h)
    assert a.json() == b.json() and a.headers["etag"] == b.headers["etag"] == '"1"'


@requires_pg
def test_missing_null_for_an_absent_note_is_valid_json_null_with_etag_zero(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    aid = make_mem_agent(client, h, {"working": {}})
    r = client.get(res_wm(aid, "someone-new"), params={"missing": "null"}, headers=h)
    assert (r.status_code, r.text.strip(), r.headers["etag"]) == (200, "null", '"0"')
    assert client.get(res_wm(aid, "someone-new"), headers=h).status_code == 404


# ---------------------------------------------------------------------------- 5. pricing parity


def _main_worker_pricing(directory: Path):
    """main's eap_worker/pricing.py (before the extraction), imported from `git show` as a module
    written into `directory` (a tmp dir, never the source tree)."""
    src = subprocess.run(
        ["git", "show", "main:apps/worker/src/eap_worker/pricing.py"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    path = directory / "_main_worker_pricing.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("main_worker_pricing", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["main_worker_pricing"] = module
    spec.loader.exec_module(module)
    # main derives these from its own location (apps/worker/src/eap_worker/pricing.py); the copy
    # lives in a tmp dir, so point them back at this repository.
    module.DEFAULT_PRICES_FILE = REPO / "config" / "model-prices.json"
    module.REPO_ROOT = REPO
    return module


P = lambda eff, i, o, **kw: {"effective_from": eff, "input_per_mtok": i, "output_per_mtok": o, **kw}  # noqa: E731
VALID = {
    "empty": {"models": {}},
    "exact beats wildcard": {"models": {"test:x": [P("2026-01-01", "3", "9")], "test:*": [P("2026-01-01", "1", "2")]}},
    "periods": {"models": {"litellm:*": [P("2026-01-01", "1", "1"), P("2026-11-01", "2.5", "7.25"), P("2027-01-01", "99", "99")]}},
    "cache prices": {"models": {"test:*": [P("2026-01-01", "2", "8", cache_read_per_mtok="0.5", cache_write_per_mtok="2.5")]}},
    "cache read only": {"models": {"test:*": [P("2026-01-01", "0.3", "1.2", cache_read_per_mtok="0.03")]}},
    "tiny and zero": {"models": {"test:*": [P("2026-01-01", "0.0000001", "0")], "litellm:*": [P("2026-01-01", "0", "0")]}},
    "numbers not strings": {"models": {"test:*": [P("2026-01-01", 2, 4)]}},
    "high precision": {"models": {"test:*": [P("2026-01-01", "0.123456789012345", "1.987654321098765")]}},
    "future only": {"models": {"test:*": [P("2099-01-01", "1", "1")]}},
}
INVALID = {
    "not json": "{nope",
    "empty text": "",
    "models is a list": '{"models": []}',
    "unknown top key": '{"models": {}, "extra": 1}',
    "unknown period key": json.dumps({"models": {"test:*": [{**P("2026-01-01", "1", "1"), "cached": "1"}]}}),
    "negative price": json.dumps({"models": {"test:*": [P("2026-01-01", "-1", "1")]}}),
    "no colon": json.dumps({"models": {"testx": [P("2026-01-01", "1", "1")]}}),
    "star provider": json.dumps({"models": {"*:x": [P("2026-01-01", "1", "1")]}}),
    "partial wildcard": json.dumps({"models": {"openai:gpt-*": [P("2026-01-01", "1", "1")]}}),
    "empty name": json.dumps({"models": {"test:": [P("2026-01-01", "1", "1")]}}),
    "bad date": json.dumps({"models": {"test:*": [P("not-a-date", "1", "1")]}}),
    "missing output": json.dumps({"models": {"test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "1"}]}}),
    "non-numeric price": json.dumps({"models": {"test:*": [P("2026-01-01", "cheap", "1")]}}),
}
MODELS = ["test:chat", "test:x", "litellm:emb", "litellm:other", "other:model", "openai:gpt-4o-mini"]
USAGES = [
    eap_pricing.Usage(), eap_pricing.Usage(1000, 50), eap_pricing.Usage(1_234_567, 89_012, 200_000, 50_000),
    eap_pricing.Usage(100, 0, 500, 700), eap_pricing.Usage(7, 3, 0, 0, measured=False),
    eap_pricing.Usage(10**9, 10**9, 10**8, 10**7),
]
DAYS = [datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 11, 1, 0, 0, tzinfo=UTC), datetime(2026, 11, 15, 12, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)]


@pytest.fixture(scope="module")
def main_pricing(tmp_path_factory):
    yield _main_worker_pricing(tmp_path_factory.mktemp("main_pricing"))
    sys.modules.pop("main_worker_pricing", None)


def _use(file_text, tmp_path, monkeypatch, name):
    path = tmp_path / f"{name}.json"
    path.write_text(file_text if isinstance(file_text, str) else json.dumps(file_text))
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(path))
    return path


def _as_main(main, u):
    return main.Usage(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.measured)


@pytest.mark.parametrize("name", sorted(VALID))
def test_worker_costs_are_byte_identical_to_mains_worker(name, main_pricing, tmp_path, monkeypatch):
    from eap_worker import pricing as new

    _use(VALID[name], tmp_path, monkeypatch, "p")
    checked = 0
    for model in MODELS:
        for usage in USAGES:
            for at in DAYS:
                old = main_pricing.price(model, _as_main(main_pricing, usage), at)
                got = new.price(model, usage, at)
                assert got == old, (name, model, usage, at)
                assert [None if c is None else format(c, "f") for c in (got[0],)] == [None if old[0] is None else format(old[0], "f")]
                checked += got[1] == "override"
    assert checked or name in ("empty", "future only")
    # The recorded row and the event fields (what the API and events carry) match too.
    for usage in USAGES:
        inp = {"model_id": "test:x", "provider_model_name": "p", "input_tokens": usage.input_tokens,
               "output_tokens": usage.output_tokens, "cache_read_tokens": usage.cache_read_tokens,
               "cache_write_tokens": usage.cache_write_tokens, "usage_measured": usage.measured}
        at = DAYS[2]
        assert json.dumps(new.usage_record("k", inp, at), default=str) == json.dumps(main_pricing.usage_record("k", inp, at), default=str)
    args = dict(model_id="litellm:emb", provider_model_name=None, input_tokens=12345, usage_measured=True, inputs=3, dimensions=1024, input_type="document", at=DAYS[2])
    assert json.dumps(new.embedding_usage_record("k", **args), default=str) == json.dumps(main_pricing.embedding_usage_record("k", **args), default=str)


@pytest.mark.parametrize("name", sorted(VALID))
def test_api_estimate_equals_the_workers_cost_string_for_override_models(name, main_pricing, tmp_path, monkeypatch):
    _use(VALID[name], tmp_path, monkeypatch, "p")
    for model in MODELS:
        for tokens in (0, 1, 999, 1_000_000, 123_456_789):
            for at in DAYS:
                cost, source, note = estimate_embedding_cost(model, tokens, at.date())
                old = main_pricing.price(model, main_pricing.Usage(input_tokens=tokens), at)
                if old[1] == "override":
                    assert (cost, source, note) == (format(old[0], "f"), "override", None), (name, model, tokens, at)
                else:
                    assert cost is None and source is None and note, (name, model, tokens)


@pytest.mark.parametrize("name", sorted(INVALID))
def test_api_and_worker_reject_the_same_price_files(name, main_pricing, tmp_path, monkeypatch):
    from eap_worker import pricing as new

    _use(INVALID[name], tmp_path, monkeypatch, "bad")
    assert isinstance(main_pricing._load_overrides(), main_pricing.InvalidPriceFile)  # main's worker rejects it
    assert isinstance(eap_pricing.load_overrides(), eap_pricing.InvalidPriceFile)
    assert isinstance(new._load_overrides(), new.InvalidPriceFile)
    assert new.price("test:x", eap_pricing.Usage(10, 10), DAYS[2]) == (None, None)
    cost, source, note = estimate_embedding_cost("test:x", 1_000_000, DAYS[2].date())
    assert (cost, source) == (None, None) and "unreadable or invalid" in note


def test_missing_and_unreachable_price_files_behave_as_on_main(main_pricing, tmp_path, monkeypatch):
    from eap_worker import pricing as new

    blocker = tmp_path / "file"
    blocker.write_text("x")
    for target in (tmp_path / "nope.json", blocker / "sub.json", tmp_path):  # missing, ENOTDIR, a directory
        monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(target))
        old, got = main_pricing._load_overrides(), eap_pricing.load_overrides()
        assert type(old).__name__ == type(got).__name__ == "InvalidPriceFile", target
        assert old.reason.split(" (")[0] == got.reason.split(" (")[0]
        assert new.price("test:x", eap_pricing.Usage(5, 5), DAYS[2]) == main_pricing.price("test:x", main_pricing.Usage(5, 5), DAYS[2]) == (None, None)
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(tmp_path / "nope.json"))
    assert "does not exist" in estimate_embedding_cost("test:x", 5, DAYS[2].date())[2]
    # A relative path resolves against the repository root, as on main; unset = the default file.
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, "config/model-prices.json")
    assert main_pricing.prices_file_path() == eap_pricing.prices_file_path() == new.prices_file_path() == REPO / "config" / "model-prices.json"
    monkeypatch.delenv(eap_pricing.PRICES_FILE_ENV)
    assert main_pricing.prices_file_path() == new.prices_file_path() == eap_pricing.prices_file_path(new.DEFAULT_PRICES_FILE)


def test_the_real_default_price_file_prices_identically(main_pricing, monkeypatch):
    from eap_worker import pricing as new

    monkeypatch.delenv(eap_pricing.PRICES_FILE_ENV, raising=False)
    for model in MODELS:
        for usage in USAGES:
            assert new.price(model, usage, DAYS[2]) == main_pricing.price(model, _as_main(main_pricing, usage), DAYS[2])


# ---------------------------------------------------------------------------- 6. dev-down.sh --all


def _dev_down_root(tmp_path):
    import shutil

    (tmp_path / "scripts").mkdir()
    (tmp_path / "bin").mkdir()
    for name in ("dev-lib.sh", "dev-down.sh"):
        shutil.copy(REPO / "scripts" / name, tmp_path / "scripts" / name)
    stub = tmp_path / "bin" / "docker"
    stub.write_text('#!/usr/bin/env bash\necho "$*" >> "$STUB_CALLS"\n[ "$1" = inspect ] && { echo eap; exit 0; }\nexit 97\n')
    stub.chmod(0o755)
    return tmp_path


def _down(root, *args):
    import os

    env = {**os.environ, "PATH": f"{root / 'bin'}:{os.environ['PATH']}", "STUB_CALLS": str(root / "calls")}
    return subprocess.run(["bash", str(root / "scripts" / "dev-down.sh"), *args], capture_output=True, text=True, env=env, timeout=60)


def test_dev_down_stop_path_never_uses_destructive_docker_commands():
    import re

    lib = (REPO / "scripts" / "dev-lib.sh").read_text()
    stop_path = lib[lib.index("docker_service_stop_command()"):]  # through stop_docker_services (the end of the stop path)
    stop_path = stop_path[: stop_path.index("stop_docker_services()") + 400]
    assert "stop" in stop_path and "docker_service_stop_command()" in stop_path
    for name, text in (("dev-lib.sh stop path", stop_path), ("dev-down.sh", (REPO / "scripts" / "dev-down.sh").read_text())):
        for line in text.splitlines():
            code = line.split("#")[0]
            assert not re.search(r"\b(down|rm|prune)\b|\s-v\b|--volumes|rmi", code.replace("dev-down", "")), (name, line)


def test_dev_down_rejects_unknown_arguments_and_skips_docker_without_all(tmp_path):
    root = _dev_down_root(tmp_path)
    bad = _down(root, "--everything")
    assert bad.returncode == 1 and "Unknown argument: --everything" in bad.stderr
    out = _down(root, "--dry-run")
    assert out.returncode == 0 and "Stopping services: worker api studio" in out.stdout
    assert not (root / "calls").exists() or (root / "calls").read_text() == ""
    both = _down(root, "--all", "--dry-run")
    assert "Stopping services: postgres lgtm worker api studio" in both.stdout
    assert "[dry-run] docker compose -p eap" in both.stdout and "stop postgres" in both.stdout


def test_dry_run_signals_nothing_and_a_real_run_stops_only_the_recorded_group(tmp_path):
    root = _dev_down_root(tmp_path)
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        started = subprocess.run(
            ["bash", "-c", f'source "{root}/scripts/dev-lib.sh"; process_start_time {proc.pid}'], capture_output=True, text=True
        ).stdout.strip()
        pid_file = root / ".dev" / "run" / "studio.pid"
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text(f"pid={proc.pid}\npgid={proc.pid}\nstart={started}\n")
        dry = _down(root, "studio", "--dry-run")
        assert f"[dry-run] kill -TERM -- -{proc.pid}" in dry.stdout, dry.stdout + dry.stderr
        assert proc.poll() is None and pid_file.exists()
        real = _down(root, "studio")
        assert real.returncode == 0 and f"Stopping process group {proc.pid}" in real.stdout
        assert proc.wait(timeout=15) is not None and not pid_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_the_shipped_dry_run_shell_test_passes():
    result = subprocess.run(["bash", str(REPO / "tests/scripts/dev-down-dry-run-test.sh")], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dev-down dry-run test: OK" in result.stdout


# ---------------------------------------------------------------------------- 1b. real API messages for Studio

import os  # noqa: E402

API_ERRORS = REPO / "apps/studio/src/test/fixtures/apiErrors.json"
_M = {"provider": "test", "name": "ticket"}
_BASE = {"name": "A", "model": _M}
_MOD = lambda model: {"type": "moderation", "config": {"model": model}}  # noqa: E731
BAD_SPECS = {
    "empty name": {**_BASE, "name": ""},
    "missing name": {"model": _M},
    "long name": {**_BASE, "name": "x" * 500},
    "no model": {"name": "A"},
    "blank provider": {**_BASE, "model": {"provider": "", "name": "x"}},
    "unknown processor type": {**_BASE, "input_processors": [{"type": "nope", "config": {}}]},
    "moderation without model": {**_BASE, "input_processors": [{"type": "moderation", "config": {}}]},
    "moderation blank provider": {**_BASE, "input_processors": [_MOD({"provider": "", "name": "m"})]},
    "output moderation blank provider": {**_BASE, "output_processors": [{"type": "pii_redact", "config": {}}, _MOD({"provider": "", "name": "m"})]},
    "max_length zero": {**_BASE, "input_processors": [{"type": "max_length", "config": {"max_chars": -1}}]},
    "bad tool id": {**_BASE, "tools": ["Bad Tool"]},
    "duplicate tool": {**_BASE, "tools": ["a.b", "a.b"]},
    "unknown key": {**_BASE, "bogus": 1},
    "bad memory scope": {**_BASE, "memory": {"working": {"scope": "nope"}}},
    "semantic recall needs a model": {**_BASE, "memory": {"semantic_recall": {"top_k": 0}}},
    "context field": {**_BASE, "context_fields": [{"name": ""}]},
    "instructions type": {**_BASE, "instructions": 5},
    "model setting type": {**_BASE, "model_settings": {"temperature": "hot"}},
    "model not allowed": {**_BASE, "model": {"provider": "nope", "name": "x"}},
    "moderation model not allowed": {**_BASE, "input_processors": [_MOD({"provider": "nope", "name": "m"})]},
    "embedding model not allowed": {**_BASE, "memory": {"semantic_recall": {"embedding_model": "nope:e"}}},
    "observer model not allowed": {**_BASE, "memory": {"observational": {"model": {"provider": "nope", "name": "o"}}}},
    "working memory schema": {**_BASE, "memory": {"working": {"json_schema": {"type": "object", "properties": {"a": {"type": "date"}}}}}},
    "template and schema": {**_BASE, "memory": {"working": {"template": "# x", "json_schema": {"type": "object", "properties": {}}}}},
    "blank template": {**_BASE, "memory": {"working": {"template": " "}}},
}


def _api_errors(client, tenant):
    h = get_auth_header(tenant, "alice")
    out = []
    for case, spec in BAD_SPECS.items():
        r = client.post("/api/v1/agents", json={"spec": spec}, headers=h)
        if r.status_code == 201:  # accepted as a draft: the platform policy is checked on publish
            r = client.post(f"/api/v1/agents/{r.json()['id']}/versions", headers=h)
        detail = r.json().get("detail")
        if isinstance(detail, list):  # exactly how Studio's apiErrorFromResponse builds ApiError.message
            message = ", ".join(str(e.get("msg", "")) for e in detail)
            locs = [[p for p in e.get("loc", []) if isinstance(p, (str, int))] for e in detail]
        else:
            message, locs = str(detail), []
        out.append({"case": case, "status": r.status_code, "message": message, "loc": locs})
    return out


def test_studio_fixture_holds_the_apis_current_error_messages(client, tenant):
    got = _api_errors(client, tenant)
    assert all(e["status"] in (400, 409, 422) for e in got), [e for e in got if e["status"] not in (400, 409, 422)]
    if os.environ.get("EAP_REGEN_API_ERRORS") == "1":
        API_ERRORS.parent.mkdir(parents=True, exist_ok=True)
        API_ERRORS.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n")
    want = json.loads(API_ERRORS.read_text())
    assert got == want, "API error messages changed: regenerate with EAP_REGEN_API_ERRORS=1"
