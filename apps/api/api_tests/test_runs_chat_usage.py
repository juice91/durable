"""Follow-up 3: GET /runs, /runs/{id}, /conversations/{id} and the trace's run carry the chat
model's own usage (chat_input_tokens, chat_output_tokens, chat_cost_usd), like the trace's split."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from .conftest import create_agent, get_auth_header, publish_version


def _row(key: str, inp: int, out: int, cost: str | None, op: str = "chat", model: str = "test:chat") -> dict:
    return {
        "request_key": key, "model_id": model, "provider_model_name": None, "input_tokens": inp,
        "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": "override",
        "created_at": datetime.now(UTC), "operation": op,
    }


def _run_with_rows(client, tenant: str, rows: list[dict]) -> tuple[str, str]:
    agent_id = create_agent(client, tenant, "alice")
    publish_version(client, agent_id, tenant, "alice")
    run_store, event_log = client.app.state.run_stores
    run_id = uuid.uuid4()
    run_store.create_run(tenant, uuid.UUID(agent_id), 1, "hi", run_id)
    for i, row in enumerate(rows):
        event_log.append(tenant, run_id, f"{row['request_key']}:final", "model_message", {"model_request": i}, row)
    return agent_id, str(run_id)


MIXED = [
    _row("model:0", 10, 2, "0.0010000000"),
    _row("model:1", 5, 3, "0.0002000000"),
    _row("moderation:input:0", 7, 1, "0.0000500000", model="test:mod"),
    _row("memory:observe:0", 40, 9, "0.0030000000", model="test:obs"),
    _row("memory:reflect:0", 4, 4, None, model="test:obs"),
    _row("embed:recall:0", 3, 0, "0.0000030000", op="embedding", model="test:hash"),
]


def _get(client, tenant, path, **params):
    response = client.get(f"/api/v1{path}", params=params, headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 200, response.text
    return response.json()


def test_chat_fields_count_only_the_agents_chat_requests(client, tenant):
    agent_id, run_id = _run_with_rows(client, tenant, MIXED)
    run = _get(client, tenant, f"/runs/{run_id}")
    assert (run["chat_input_tokens"], run["chat_output_tokens"], run["chat_cost_usd"]) == (15, 5, "0.0012000000")
    listed = next(r for r in _get(client, tenant, "/runs", agent_id=agent_id)["items"] if r["run_id"] == run_id)
    assert {k: listed[k] for k in ("chat_input_tokens", "chat_output_tokens", "chat_cost_usd")} == {
        "chat_input_tokens": 15, "chat_output_tokens": 5, "chat_cost_usd": "0.0012000000",
    }


def test_matches_the_trace_split(client, tenant):
    _agent, run_id = _run_with_rows(client, tenant, MIXED)
    trace = _get(client, tenant, f"/runs/{run_id}/trace")
    run = trace["run"]
    assert (run["chat_input_tokens"], run["chat_output_tokens"], run["chat_cost_usd"]) == (
        trace["totals"]["chat_input_tokens"], trace["totals"]["chat_output_tokens"], trace["totals"]["chat_cost_usd"],
    )


def test_unpriced_chat_request_nulls_the_cost_and_no_rows_nulls_everything(client, tenant):
    _a, unpriced = _run_with_rows(client, tenant, [_row("model:0", 10, 2, None), _row("moderation:input:0", 1, 1, "0.1")])
    run = _get(client, tenant, f"/runs/{unpriced}")
    assert (run["chat_input_tokens"], run["chat_output_tokens"], run["chat_cost_usd"]) == (10, 2, None)
    _a, only_moderation = _run_with_rows(client, tenant, [_row("moderation:input:0", 7, 1, "0.0000500000")])
    run = _get(client, tenant, f"/runs/{only_moderation}")
    assert (run["chat_input_tokens"], run["chat_output_tokens"], run["chat_cost_usd"]) == (0, 0, "0")
    _a, none = _run_with_rows(client, tenant, [])
    run = _get(client, tenant, f"/runs/{none}")
    assert (run["chat_input_tokens"], run["chat_output_tokens"], run["chat_cost_usd"]) == (None, None, None)


def test_another_tenants_rows_never_count(client, tenant):
    _a, run_id = _run_with_rows(client, tenant, MIXED)
    run_store, _ = client.app.state.run_stores
    assert run_store.chat_usage("someone-else", [run_id]) == {}
    assert run_store.chat_usage(tenant, []) == {}


def test_a_page_of_runs_is_one_aggregate_query(client, tenant):
    agent_id = None
    for _ in range(3):
        agent_id, _run = _run_with_rows(client, tenant, MIXED[:2])
    run_store, _ = client.app.state.run_stores
    calls = []
    original = run_store.chat_usage

    def counting(tenant_id, run_ids):
        calls.append(list(run_ids))
        return original(tenant_id, run_ids)

    run_store.chat_usage = counting
    try:
        items = _get(client, tenant, "/runs")["items"]
    finally:
        del run_store.chat_usage
    assert len(calls) == 1 and len(calls[0]) == len(items) >= 3
    assert all(i["chat_input_tokens"] == 15 for i in items if i["agent_id"] is not None and i["chat_input_tokens"] is not None)


def test_conversation_turns_carry_the_chat_fields(client, tenant):
    agent_id = create_agent(client, tenant, "alice")
    publish_version(client, agent_id, tenant, "alice")
    conv = client.post(f"/api/v1/agents/{agent_id}/conversations", json={}, headers=get_auth_header(tenant, "alice"))
    assert conv.status_code in (200, 201), conv.text
    body = _get(client, tenant, f"/conversations/{conv.json()['conversation_id'] if 'conversation_id' in conv.json() else conv.json()['id']}")
    for run in body["runs"]:
        assert {"chat_input_tokens", "chat_output_tokens", "chat_cost_usd"} <= set(run)


def _trace_chat(client, tenant, run_id):
    t = _get(client, tenant, f"/runs/{run_id}/trace")["totals"]
    return (t["chat_input_tokens"], t["chat_output_tokens"], t["chat_cost_usd"])


def _run_chat(client, tenant, run_id):
    r = _get(client, tenant, f"/runs/{run_id}")
    return (r["chat_input_tokens"], r["chat_output_tokens"], r["chat_cost_usd"])


def test_a_chat_request_without_a_usage_row_nulls_the_cost_like_the_trace(client, tenant):
    """Review minor 2: a reserved, not completed request (a model_message event, no usage row)."""
    _a, run_id = _run_with_rows(client, tenant, MIXED)
    _, event_log = client.app.state.run_stores
    event_log.append(tenant, uuid.UUID(run_id), "model:9:final", "model_message", {"model_request": 9})
    assert _run_chat(client, tenant, run_id) == _trace_chat(client, tenant, run_id) == (15, 5, None)


def test_only_unrecorded_chat_requests_null_the_tokens_like_the_trace(client, tenant):
    _a, run_id = _run_with_rows(client, tenant, [_row("moderation:input:0", 7, 1, "0.0000500000", model="test:mod")])
    _, event_log = client.app.state.run_stores
    event_log.append(tenant, uuid.UUID(run_id), "model:0:final", "model_message", {"model_request": 0})
    assert _run_chat(client, tenant, run_id) == _trace_chat(client, tenant, run_id) == (None, None, None)
    # Events of other operations without a row are not chat requests.
    _a, other = _run_with_rows(client, tenant, MIXED)
    event_log.append(tenant, uuid.UUID(other), "moderation:output:0:model:0:final", "model_message", {})
    assert _run_chat(client, tenant, other) == _trace_chat(client, tenant, other) == (15, 5, "0.0012000000")
