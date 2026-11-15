"""AC.5 limits and fallback runtime (agent capabilities milestone): implementer's focused tests.

Unit: `usage_limits_for`, `fallback_chain`, the resolver's FallbackModel, answering-model pricing,
`classify` data for the new and extended codes. In-process worker: request and tool-call limits,
the cost cap (`eap.check_budget`), fallback answer and attribution, fallback exhausted, a fallback
off the allowlist, an override disabling fallback, and EventLog.append with a list of usage rows.
"""

from __future__ import annotations

import pickle
import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.exceptions import FallbackExceptionGroup, ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import ModelRequestAttempt, ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import text

from eap_schemas import AgentSpec
from eap_schemas.limits import RunBudgetExceeded
from eap_schemas.models_policy import ModelPolicy
from eap_worker.errors import classify
from eap_worker.models import fallback_chain, make_resolver
from eap_worker.pricing import PRICE_SOURCE_FALLBACK_UNVERIFIED, answering_index, fallback_usage
from eap_worker.workflow import usage_limits_for
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_processors_runtime import BASELINE

MODEL = "eap-agent__model.request_stream"
GET_TOOLS = "eap-agent__dynamic_toolset__eap-spec-tools.get_tools"


def spec_with(model: str = "echo", tools: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    return {**ticket_spec(tools=tools, model=model), **extra}


def fallbacks(*names: str) -> list[dict[str, str]]:
    return [{"provider": "test", "name": n} for n in names]


def run(worker, client, tenant, spec, input_text="hi", **kw):
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=input_text,
                         app_version=APP_VERSION, **kw)
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


def usage_rows(engine, run_id) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(
                text("SELECT request_key, model_id, cost_usd, price_source, operation FROM run_model_requests"
                     " WHERE run_id = :r ORDER BY request_key"),
                {"r": str(run_id)},
            )
        ]


def events_by_key(engine, run_id) -> dict[str, tuple[str, dict]]:
    return {key: (type_, data) for _seq, key, type_, data in event_rows(engine, run_id)}


# --------------------------------------------------------------------------- unit


def test_usage_limits_only_for_native_caps():
    assert usage_limits_for(AgentSpec.model_validate(spec_with())) is None
    assert usage_limits_for(AgentSpec.model_validate(spec_with(limits={"max_cost_usd": "1"}))) is None
    limits = usage_limits_for(AgentSpec.model_validate(spec_with(limits={"max_tool_calls": 3})))
    assert limits is not None and limits.tool_calls_limit == 3 and limits.request_limit == 50
    limits = usage_limits_for(AgentSpec.model_validate(spec_with(limits={"max_requests": 2, "max_total_tokens": 9})))
    # Token caps are the workflow's own check (after the response is recorded), never passed natively.
    assert limits.request_limit == 2 and limits.total_tokens_limit is None
    from eap_worker.workflow import token_limits_for

    assert token_limits_for(AgentSpec.model_validate(spec_with(limits={"max_requests": 2, "max_total_tokens": 9}))) == {
        "max_total_tokens": 9
    }
    assert token_limits_for(AgentSpec.model_validate(spec_with())) == {}


def test_fallback_chain_only_for_the_primary():
    spec = AgentSpec.model_validate(spec_with(model="fail-503", fallback_models=fallbacks("echo", "history")))
    assert fallback_chain(spec, "test:fail-503") == ("test:fail-503", "test:echo", "test:history")
    assert fallback_chain(spec, "test:ticket") == ("test:ticket",)  # an override
    assert fallback_chain(AgentSpec.model_validate(spec_with()), "test:echo") == ("test:echo",)
    assert fallback_chain(None, "test:echo") == ("test:echo",)


class _Ctx:
    def __init__(self, spec):
        self.deps = type("Deps", (), {"spec": spec})()


def test_resolver_builds_a_checked_fallback_model_for_eap_agent_only(worker_settings):
    spec = AgentSpec.model_validate(spec_with(model="fail-503", fallback_models=fallbacks("echo")))
    resolve = make_resolver(lambda: worker_settings, fallback=True)
    model = resolve(_Ctx(spec), "test:fail-503")
    assert isinstance(model, FallbackModel) and len(model.models) == 2
    assert not isinstance(resolve(_Ctx(spec), "test:ticket"), FallbackModel)  # override: bare model
    # Without `fallback` (observer, moderator, judge): one model, whatever the deps.
    assert not isinstance(make_resolver(lambda: worker_settings)(_Ctx(spec), "test:fail-503"), FallbackModel)
    # Every member passes the allowlist.
    narrow = replace(worker_settings, allowed_models=ModelPolicy.parse("test:fail-503"))
    from eap_schemas.models_policy import ModelNotAllowed

    with pytest.raises(ModelNotAllowed):
        make_resolver(lambda: narrow, fallback=True)(_Ctx(spec), "test:fail-503")


def _attempt(name: str, usage: RequestUsage | None = None) -> ModelRequestAttempt:
    from datetime import datetime, timezone

    return ModelRequestAttempt(
        model_name=name, outcome="error", error="ModelHTTPError: status_code: 503, secret body",
        timestamp=datetime.now(timezone.utc), duration=timedelta(seconds=0), usage=usage,
    )


def test_answering_model_pricing_and_failed_attempts():
    chain = ("test:fail-503", "test:priced")
    response = ModelResponse(parts=[TextPart("ok")], model_name="scripted-priced",
                             usage=RequestUsage(input_tokens=1000, output_tokens=500))
    response.failed_attempts = [_attempt("scripted-fail-503", RequestUsage(input_tokens=10, output_tokens=0))]
    usage, extra, failed = fallback_usage(chain, response)
    assert usage["model_id"] == "test:priced" and "price_source_override" not in usage
    assert extra == {
        "requested_model_id": "test:fail-503", "fallback_index": 1,
        "failed_attempts": [{"model_id": "test:fail-503", "outcome": "error", "error_type": "ModelHTTPError", "http_status": 503}],
    }
    assert [f["k"] for f in failed] == [0] and failed[0]["model_id"] == "test:fail-503"
    assert "secret" not in repr(extra)
    # The provider names another chain member: the row keeps the chain's id, marked unverified.
    assert answering_index(("litellm:a", "litellm:b"), "a", 1) == (1, False)
    assert answering_index(("litellm:a", "litellm:b"), "b", 1) == (1, True)
    assert answering_index(("litellm:a", "litellm:b"), "other", 0) == (0, True)
    response.model_name = "fail-503"
    usage, _extra, _failed = fallback_usage(chain, response)
    assert usage["price_source_override"] == PRICE_SOURCE_FALLBACK_UNVERIFIED and usage["model_id"] == "test:priced"


def test_classify_limits_budget_and_fallback_exhausted():
    f = classify(UsageLimitExceeded("The next request would exceed the request_limit of 2"))
    assert (f.code, f.data) == ("usage_limit_exceeded", {"limit": "max_requests", "value": 2})
    f = classify(UsageLimitExceeded("The next tool call(s) would exceed the tool_calls_limit of 3 (tool_calls=4)."))
    assert f.data == {"limit": "max_tool_calls", "value": 3}
    f = classify(UsageLimitExceeded("Exceeded the output_tokens_limit of 9 (output_tokens=10)"))
    assert f.data == {"limit": "max_output_tokens", "value": 9}
    budget = pickle.loads(pickle.dumps(RunBudgetExceeded(Decimal("0.06"), Decimal("0.05"))))
    f = classify(budget)
    assert f.code == "run_budget_exceeded" and f.data == {"limit": "max_cost_usd", "value": "0.05", "spent_usd": "0.06"}
    assert f.error == "RunBudgetExceeded: the run reached its cost limit"
    group = FallbackExceptionGroup("All models from FallbackModel failed", [ModelHTTPError(503, "m", {"x": "secret"})])
    for exc in (group, pickle.loads(pickle.dumps(group))):
        f = classify(exc)
        assert (f.code, f.data) == ("fallback_exhausted", {"cause": "model_http_error"})
        assert "secret" not in f.error


# --------------------------------------------------------------------------- in-process worker


def test_request_and_tool_call_limits(worker, client, tenant):
    for limits, expected in (({"max_requests": 2}, ("max_requests", 2)), ({"max_tool_calls": 2}, ("max_tool_calls", 2))):
        run_id, row = run(worker, client, tenant, spec_with(model="loop", tools=("clock.now",), limits=limits))
        assert row["status"] == "failed" and row["error"] == "UsageLimitExceeded: the run exceeded a usage limit", row
        end = events_by_key(worker.engine, run_id)["end"]
        assert end[0] == "run_failed" and end[1]["error_code"] == "usage_limit_exceeded"
        assert (end[1]["limit"], end[1]["value"]) == expected


def test_cost_cap_stops_before_the_request_over_it(worker, client, tenant):
    # test:priced costs USD 0.02 a request: 0.02, 0.04 pass the 0.05 cap, 0.06 does not.
    spec = spec_with(model="priced", tools=("clock.now",), limits={"max_cost_usd": "0.05"})
    run_id, row = run(worker, client, tenant, spec, '{"rounds": 5}')
    assert row["status"] == "failed" and row["error"] == "RunBudgetExceeded: the run reached its cost limit", row
    end = events_by_key(worker.engine, run_id)["end"][1]
    assert end["error_code"] == "run_budget_exceeded" and end["limit"] == "max_cost_usd"
    assert (end["value"], Decimal(end["spent_usd"])) == ("0.05", Decimal("0.06"))
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:1", "model:2"]
    steps = step_names(worker.settings.dbos_system_schema, run_id)
    assert steps.count("eap.check_budget") == 3 and steps.count(MODEL) == 3
    # check_budget sits right before each model request n >= 1 (after its get_tools); the last one
    # refuses, so `end` follows it.
    checks = [i for i, name in enumerate(steps) if name == "eap.check_budget"]
    assert all(steps[i + 1:i + 3] == [GET_TOOLS, MODEL] for i in checks[:-1]), steps
    assert steps[checks[-1] + 1:] == ["eap.append_event", "eap.finish_run"], steps


def test_fallback_answers_and_is_attributed(worker, client, tenant):
    spec = spec_with(model="fail-503", fallback_models=fallbacks("echo"))
    run_id, row = run(worker, client, tenant, spec, "hello")
    assert row["status"] == "completed" and row["output"] == "Echo: hello", row
    message = events_by_key(worker.engine, run_id)["model:0:final"][1]
    assert message["model_id"] == "test:echo" and message["requested_model_id"] == "test:fail-503"
    assert message["fallback_index"] == 1
    assert message["failed_attempts"] == [{"model_id": "test:fail-503", "outcome": "error", "error_type": "ModelHTTPError", "http_status": 503}]
    assert [(r["request_key"], r["model_id"]) for r in usage_rows(worker.engine, run_id)] == [("model:0", "test:echo")]
    # No step of its own: the plain layout.
    assert step_names(worker.settings.dbos_system_schema, run_id) == BASELINE["echo"]


def test_all_models_failing_is_fallback_exhausted(worker, client, tenant):
    spec = spec_with(model="fail-503", fallback_models=fallbacks("fail-400"))
    run_id, row = run(worker, client, tenant, spec)
    assert row["status"] == "failed", row
    end = events_by_key(worker.engine, run_id)["end"][1]
    assert end["error_code"] == "fallback_exhausted" and end["cause"] == "model_http_error"
    assert end["error"] == "FallbackExceptionGroup: every model of the agent's fallback chain failed"


def test_fallback_off_the_allowlist_fails_closed_at_run_start(worker, client, tenant):
    spec = spec_with(fallback_models=[{"provider": "litellm", "name": "not-allowed-here"}])
    run_id, row = run(worker, client, tenant, spec)
    assert row["status"] == "failed" and "ModelNotAllowed" in row["error"], row
    assert [k for _s, k, _t, _d in event_rows(worker.engine, run_id)] == ["start", "end"]


def test_override_disables_fallback(worker, client, tenant):
    spec = spec_with(model="echo", fallback_models=fallbacks("history"))
    run_id, agent_id = uuid.uuid4(), uuid.uuid4()
    worker.run_store.create_run(tenant, agent_id, 1, "hi", run_id, model_id="test:fail-503", model_overridden=True)
    run_id, row = run(worker, client, tenant, spec, run_id=run_id, agent_id=agent_id, create_row=False)
    assert row["status"] == "failed", row
    events = events_by_key(worker.engine, run_id)
    assert events["start"][1]["fallback"] == "disabled_by_override"
    assert events["end"][1]["error_code"] == "model_http_error"  # no fallback was tried


def test_event_log_append_accepts_a_row_list(worker, tenant):
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "hi", run_id)

    def row(key: str, cost: str) -> dict[str, Any]:
        return {"request_key": key, "model_id": "test:echo", "provider_model_name": None, "input_tokens": 1,
                "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
                "cost_usd": Decimal(cost), "price_source": "override"}

    worker.event_log.append(tenant, run_id, "model:0:final", "model_message", {}, [row("model:0", "0.5"), row("model:0:failed:0", "0.25")])
    worker.event_log.append(tenant, run_id, "model:1:final", "model_message", {}, row("model:1", "1"))
    # A repeated key writes no row.
    worker.event_log.append(tenant, run_id, "model:1:final", "model_message", {}, [row("model:1:failed:0", "9")])
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:0:failed:0", "model:1"]
    assert worker.run_store.recorded_cost_usd(tenant, run_id) == Decimal("1.75")
    assert worker.run_store.recorded_cost_usd("other-tenant", run_id) == Decimal(0)


def test_failed_attempt_error_text_is_scrubbed():
    from eap_worker.pricing import safe_attempt_error, scrub_failed_attempts

    raw = "ModelHTTPError: status_code: 503, model_name: m, body: {'error': 'secret prompt'}"
    assert safe_attempt_error(raw) == "ModelHTTPError: HTTP 503"
    assert safe_attempt_error("ModelAPIError: connection to secret host failed") == "ModelAPIError"
    assert safe_attempt_error("not an identifier!: x") == "Exception" and safe_attempt_error(None) is None
    messages = [{"kind": "response", "failed_attempts": [{"model_name": "m", "outcome": "error", "error": raw}]},
                {"kind": "request", "parts": []}]
    cleaned = scrub_failed_attempts(messages)
    assert cleaned[0]["failed_attempts"] == [{"model_name": "m", "outcome": "error", "error": "ModelHTTPError: HTTP 503"}]
    assert cleaned[1] == messages[1] and "secret" in repr(messages)  # pure: the input is untouched
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai.messages import ModelResponse, TextPart

    response = ModelResponse(parts=[TextPart("ok")])
    response.failed_attempts = [_attempt("scripted-fail-503")]
    dumped = scrub_failed_attempts(ModelMessagesTypeAdapter.dump_python([response], mode="json"))
    assert "secret" not in repr(dumped)
    assert ModelMessagesTypeAdapter.validate_python(dumped)[0].failed_attempts[0].error == "ModelHTTPError: HTTP 503"


def test_stored_messages_carry_no_provider_error_text(worker, client, tenant):
    spec = spec_with(model="fail-503", fallback_models=fallbacks("echo"))
    run_id, row = run(worker, client, tenant, spec, "hello")
    assert row["status"] == "completed", row
    with worker.engine.connect() as conn:
        stored = repr(conn.execute(text("SELECT messages FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar_one())
    assert "failed_attempts" in stored and "scripted failure" not in stored and "status_code" not in stored
    message = events_by_key(worker.engine, run_id)["model:0:final"][1]
    assert message["failed_attempts"][0]["http_status"] == 503


# --------------------------------------------------------------------------- AC.5 review fixes

from worker_tests.test_processors_runtime import PII, echo_spec, moderation  # noqa: E402
from worker_tests.test_retrieve import PRINTER, ingest, kb_tenant, make_kb, rag_spec  # noqa: E402,F401 - fixture
from worker_tests.test_semantic_recall import recall_spec  # noqa: E402
from worker_tests.test_step_layout_ac import mem_tenant  # noqa: E402,F401 - fixture
from worker_tests.test_working_memory import conversation, turn  # noqa: E402

CALL_TOOL = "eap-agent__dynamic_toolset__eap-spec-tools.call_tool"
HANDLER = "eap-agent__event_stream_handler"
EVENT = "eap.append_event"
# Output processors (no limits, no fallback) on a response with text AND a tool call: the processors
# check its text (no moderation: it has a tool call), the tool runs, then the final answer is checked
# (moderation included). Recorded with the AC.5 review fix.
GOLDEN_TEXT_TOOL = [
    "eap.start_run", EVENT, GET_TOOLS, MODEL, "eap.output_processors", EVENT, EVENT, EVENT, HANDLER, CALL_TOOL,
    HANDLER, EVENT, GET_TOOLS, MODEL, "eap.output_processors", "eap-moderator__model.request", EVENT, EVENT, EVENT,
    EVENT, "eap.finish_run",
]


def step_outputs(schema: str, run_id, function_name: str) -> list[Any]:
    import base64

    import psycopg

    from worker_tests.conftest import DBOS_DB_URL

    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        rows = conn.execute(
            f'SELECT output FROM "{schema}".operation_outputs WHERE workflow_uuid = %s AND function_name = %s'
            " ORDER BY function_id",
            (str(run_id), function_name),
        ).fetchall()
    return [pickle.loads(base64.b64decode(r[0])) for r in rows if r[0] is not None]


def test_output_processors_on_a_text_and_tool_call_response(worker, client, tenant):
    """Regression (review BLOCKING 1): `chain` (fallback) was shadowed by a ChainRun on the second response."""
    spec = {**echo_spec(output_processors=[PII, moderation()], model="text-tool"), "tools": ["clock.now"]}
    run_id, row = run(worker, client, tenant, spec, "hi")
    assert row["status"] == "completed" and row["output"] == "Done.", row
    events = events_by_key(worker.engine, run_id)
    assert events["model:0:final"][1]["text"] == "Let me check."
    assert step_names(worker.settings.dbos_system_schema, run_id) == GOLDEN_TEXT_TOOL
    # The same shape with fallback and limits keeps the same steps and still prices per response.
    spec = {**spec, "fallback_models": fallbacks("echo"), "limits": {"max_requests": 5}}
    run_id, row = run(worker, client, tenant, spec, "hi")
    assert row["status"] == "completed", row
    assert step_names(worker.settings.dbos_system_schema, run_id) == GOLDEN_TEXT_TOOL
    assert [r["model_id"] for r in usage_rows(worker.engine, run_id) if r["request_key"].startswith("model:")] == [
        "test:text-tool", "test:text-tool"
    ]


def test_a_token_limit_breach_keeps_the_billed_request(worker, client, tenant):
    """Review MAJOR 1: test:priced answers 500 output tokens; the cap of 499 fails the run AFTER the
    request's model_message and usage row are written."""
    run_id, row = run(worker, client, tenant, spec_with(model="priced", limits={"max_output_tokens": 499}), "hi")
    assert row["status"] == "failed" and row["error"] == "UsageLimitExceeded: the run exceeded a usage limit", row
    events = events_by_key(worker.engine, run_id)
    assert list(events) == ["start", "model:0:final", "end"]
    assert (events["end"][1]["limit"], events["end"][1]["value"]) == ("max_output_tokens", 499)
    assert [(r["request_key"], r["cost_usd"]) for r in usage_rows(worker.engine, run_id)] == [("model:0", Decimal("0.0200000000"))]
    assert row["cost_usd"] == Decimal("0.0200000000")
    # At the cap: not over it, so the run completes.
    _run_id, row = run(worker, client, tenant, spec_with(model="priced", limits={"max_output_tokens": 500}), "hi")
    assert row["status"] == "completed", row
    # Total tokens (1500 per request): the breach of a total cap of 1499 also keeps its row.
    run_id, row = run(worker, client, tenant, spec_with(model="priced", limits={"max_total_tokens": 1499}), "hi")
    assert events_by_key(worker.engine, run_id)["end"][1]["limit"] == "max_total_tokens"
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0"]


def test_a_token_breach_before_tools_runs_no_tool(worker, client, tenant):
    spec = spec_with(model="priced", tools=("clock.now",), limits={"max_input_tokens": 999})
    run_id, row = run(worker, client, tenant, spec, '{"rounds": 2}')
    assert row["status"] == "failed", row
    keys = list(events_by_key(worker.engine, run_id))
    assert keys[:2] == ["start", "model:0:final"] and keys[-1] == "end"
    assert not any(k.startswith("tool:") for k in keys)  # the breaching response's tool call never runs


def test_limit_data_is_structured():
    from types import SimpleNamespace

    from eap_schemas.limits import UsageLimitsSpec
    from eap_worker.errors import check_token_limits, describe_usage_limit

    with pytest.raises(UsageLimitExceeded) as info:
        check_token_limits({"max_output_tokens": 9}, SimpleNamespace(output_tokens=10))
    assert classify(info.value).data == {"limit": "max_output_tokens", "value": 9}
    check_token_limits({"max_output_tokens": 9}, SimpleNamespace(output_tokens=9))  # at the cap: fine
    # A Pydantic AI error with unfamiliar wording still gets its limit from the run's counters.
    exc = UsageLimitExceeded("some future wording")
    describe_usage_limit(exc, UsageLimitsSpec(max_tool_calls=3), SimpleNamespace(requests=2))
    assert classify(exc).data == {"limit": "max_tool_calls", "value": 3}
    exc = UsageLimitExceeded("some future wording")
    describe_usage_limit(exc, None, SimpleNamespace(requests=50))
    assert classify(exc).data == {"limit": "max_requests", "value": 50}


def test_a_blocked_fallback_answer_keeps_the_fallback_fields(worker, client, tenant):
    """Review MINOR 2."""
    spec = {**echo_spec(output_processors=[moderation()], model="fail-503"), "fallback_models": fallbacks("echo")}
    run_id, row = run(worker, client, tenant, spec, "FLAG_ME")
    assert row["status"] == "failed", row
    message = events_by_key(worker.engine, run_id)["model:0:final"][1]
    assert message["blocked"] is True and message["model_id"] == "test:echo"
    assert (message["requested_model_id"], message["fallback_index"]) == ("test:fail-503", 1)
    assert message["failed_attempts"][0]["model_id"] == "test:fail-503"


def test_attempts_map_to_their_model_by_name():
    """Review MINOR 5."""
    from eap_worker.pricing import attempt_model_id

    chain = ("litellm:a", "litellm:b", "litellm:c")
    assert attempt_model_id(chain, 0, "a") == "litellm:a"
    assert attempt_model_id(chain, 1, "a") == "litellm:a"  # an extra (pinned) attempt shifted positions
    assert attempt_model_id(chain, 1, "scripted-x") == "litellm:b"  # no name: by position
    assert attempt_model_id(chain, 9, None) == "litellm:c"


# --------------------------------------------------------------------------- what is counted (review MINOR 4)


def test_retrieve_counts_as_one_tool_call_and_its_embedding_as_no_request(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    # One retrieve call, two model requests: allowed by max_tool_calls=1 and max_requests=2 (its query
    # embedding is neither a request nor tokens of the agent).
    spec = {**rag_spec({"id": kb["id"]}), "limits": {"max_tool_calls": 1, "max_requests": 2}}
    run_id, row = run(worker, client, kb_tenant, spec, "toner light")
    assert row["status"] == "completed", row
    keys = [r["request_key"] for r in usage_rows(worker.engine, run_id)]
    assert any(k.startswith("embed:retrieve:") for k in keys) and "model:1" in keys
    # Two retrieve calls in one response: over max_tool_calls=1.
    spec = {**rag_spec({"id": kb["id"]}, model="retrieve-multi"), "limits": {"max_tool_calls": 1}}
    run_id, row = run(worker, client, kb_tenant, spec, '{"rounds": [["toner", "paper"]]}')
    assert row["status"] == "failed", row
    end = events_by_key(worker.engine, run_id)["end"][1]
    assert (end["error_code"], end["limit"], end["value"]) == ("usage_limit_exceeded", "max_tool_calls", 1)


def test_the_cost_cap_counts_in_turn_embeddings(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    spec = {**rag_spec({"id": kb["id"]}), "limits": {"max_cost_usd": "1000"}}
    run_id, row = run(worker, client, kb_tenant, spec, "toner light")
    assert row["status"] == "completed", row
    rows = {r["request_key"]: r for r in usage_rows(worker.engine, run_id)}
    embed = [r for k, r in rows.items() if k.startswith("embed:retrieve:")]
    assert len(embed) == 1 and embed[0]["cost_usd"] > 0  # test:hash is priced in the test price file
    # check_budget before request 1 saw model:0 AND the retrieve embedding (recorded before it).
    spent = step_outputs(worker.settings.dbos_system_schema, run_id, "eap.check_budget")
    assert [Decimal(s) for s in spent] == [rows["model:0"]["cost_usd"] + embed[0]["cost_usd"]]


def test_post_turn_rows_never_fail_an_answered_turn(worker, client, mem_tenant):
    # test:priced costs USD 0.02 for its single request, already over the 0.0001 cap; a one-request
    # turn is never checked, and the post-turn index row (embed:index:) is written after the turn
    # completed: the turn stays completed and answered.
    conv = conversation(worker, mem_tenant)
    spec = {**recall_spec(model="priced"), "limits": {"max_cost_usd": "0.0001"}}
    run_id, row = turn(worker, client, mem_tenant, conv, "hello", spec)
    from worker_tests.test_observational_memory import wait_workflow

    wait_workflow(worker.settings.dbos_system_schema, run_id)
    row = worker.run_store.get_run(mem_tenant, run_id)
    assert row["status"] == "completed" and row["output"] == "Echo: hello", row
    keys = [r["request_key"] for r in usage_rows(worker.engine, run_id)]
    assert "model:0" in keys and any(k.startswith("embed:index:") for k in keys)
    assert row["cost_usd"] > Decimal("0.0001")


# --------------------------------------------------------------------------- AC.5 re-review fixes


def test_a_tool_call_breach_on_the_last_allowed_request_names_max_tool_calls(worker, client, tenant):
    """Re-review MAJOR: max_requests=2, max_tool_calls=1; request 2 wants a 2nd tool call, so the
    tool-call cap trips while requests are AT (not over) their cap."""
    from types import SimpleNamespace

    from eap_schemas.limits import UsageLimitsSpec
    from eap_worker.errors import describe_usage_limit

    exc = UsageLimitExceeded("The next tool call(s) would exceed the tool_calls_limit of 1 (tool_calls=2).")
    describe_usage_limit(exc, UsageLimitsSpec(max_requests=2, max_tool_calls=1), SimpleNamespace(requests=2, tool_calls=1))
    assert classify(exc).data == {"limit": "max_tool_calls", "value": 1}
    exc = UsageLimitExceeded("The next request would exceed the request_limit of 2")
    describe_usage_limit(exc, UsageLimitsSpec(max_requests=2, max_tool_calls=1), SimpleNamespace(requests=2, tool_calls=1))
    assert classify(exc).data == {"limit": "max_requests", "value": 2}

    spec = spec_with(model="loop", tools=("clock.now",), limits={"max_requests": 2, "max_tool_calls": 1})
    run_id, row = run(worker, client, tenant, spec)
    assert row["status"] == "failed", row
    end = events_by_key(worker.engine, run_id)["end"][1]
    assert (end["error_code"], end["limit"], end["value"]) == ("usage_limit_exceeded", "max_tool_calls", 1)
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:1"]


def test_a_token_breach_never_runs_the_output_processors(worker, client, tenant):
    """Re-review MINOR: the cap is decided before the processors, so no (billed) moderation request;
    the response's usage row is kept and its unchecked text is never stored."""
    spec = {**echo_spec(output_processors=[PII, moderation()], model="priced"), "limits": {"max_output_tokens": 499}}
    run_id, row = run(worker, client, tenant, spec, "mail ada@example.com")
    assert row["status"] == "failed", row
    events = events_by_key(worker.engine, run_id)
    assert list(events) == ["start", "model:0:final", "end"]
    message = events["model:0:final"][1]
    assert message["text"] == "" and message["withheld"] == "usage_limit_exceeded" and "message" not in message
    assert "ada@example.com" not in repr(message)  # (run_started holds the input: no input processors)
    assert (events["end"][1]["limit"], events["end"][1]["value"]) == ("max_output_tokens", 499)
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0"]  # no moderation row
    steps = step_names(worker.settings.dbos_system_schema, run_id)
    assert "eap.output_processors" not in steps and "eap-moderator__model.request" not in steps
