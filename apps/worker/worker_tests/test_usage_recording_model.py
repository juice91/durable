"""LP.8 review follow-up: direct tests of the in-step usage recording (UsageRecordingModel,
_recording_resolver, _record_usage_in_model_step) and of the model_message event following the
stored usage row. All but the last test use no database."""

from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from eap_worker import workflow
from eap_worker.workflow import (
    _MODEL_REQUEST,
    UsageRecordingModel,
    _record_usage_in_model_step,
    _recording_resolver,
    _same_usage,
    _usage_fields_from_row,
    append_event_step,
)

MESSAGES = [ModelRequest(parts=[UserPromptPart("hi")])]
CURRENT = {"run_id": str(uuid.uuid4()), "tenant_id": "t_unit", "model_chain": ("test:echo",), "model_id": "test:echo", "n": 2}


def _answer(_messages: list[Any], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("ok")], usage=RequestUsage(input_tokens=11, output_tokens=7))


async def _stream(_messages: list[Any], _info: AgentInfo):
    yield "o"
    yield "k"


@pytest.fixture
def writes(monkeypatch) -> list[tuple[str, str, list[dict[str, Any]]]]:
    captured: list[tuple[str, str, list[dict[str, Any]]]] = []
    monkeypatch.setattr(workflow, "_write_request_usage", lambda t, r, rows: captured.append((t, r, rows)))
    return captured


def _with_request(coro_fn, current: dict[str, Any] | None = CURRENT):
    async def run():
        token = _MODEL_REQUEST.set(current)
        try:
            return await coro_fn()
        finally:
            _MODEL_REQUEST.reset(token)

    return asyncio.run(run())


def test_request_records_the_response_usage_under_model_n(writes):
    model = UsageRecordingModel(FunctionModel(_answer))
    response = _with_request(lambda: model.request(MESSAGES, None, ModelRequestParameters()))
    assert response.parts[0].content == "ok"
    ((tenant, run, rows),) = writes
    assert (tenant, run) == ("t_unit", CURRENT["run_id"])
    ((row,),) = [rows]
    assert row["request_key"] == "model:2" and row["model_id"] == "test:echo"
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)


def test_stream_records_only_after_the_stream_is_closed(writes):
    model = UsageRecordingModel(FunctionModel(stream_function=_stream))

    async def consume() -> int:
        async with model.request_stream(MESSAGES, None, ModelRequestParameters()) as streamed:
            async for _ in streamed:
                pass
            seen_inside = len(writes)
        return seen_inside

    assert _with_request(consume) == 0
    ((_, _, rows),) = writes
    assert rows[0]["request_key"] == "model:2" and rows[0]["output_tokens"] >= 1


def test_nothing_is_recorded_outside_a_run_or_for_a_suspended_response(writes):
    model = UsageRecordingModel(FunctionModel(_answer))
    _with_request(lambda: model.request(MESSAGES, None, ModelRequestParameters()), current=None)
    suspended = ModelResponse(parts=[], usage=RequestUsage(input_tokens=5), state="suspended")
    _with_request(lambda: _record_usage_in_model_step(suspended))
    assert writes == []


def test_a_failed_write_is_logged_and_never_raises(monkeypatch, caplog):
    def boom(*_args: Any) -> None:
        raise ConnectionError("db down")

    monkeypatch.setattr(workflow, "_write_request_usage", boom)
    model = UsageRecordingModel(FunctionModel(_answer))
    with caplog.at_level(logging.WARNING, logger="eap_worker"):
        response = _with_request(lambda: model.request(MESSAGES, None, ModelRequestParameters()))
    assert response.parts[0].content == "ok"
    assert [r.getMessage() for r in caplog.records if "usage_in_step_failed" in r.getMessage()]
    assert "db down" not in caplog.text


def test_recording_resolver_wraps_resolved_and_inferred_models():
    inner = FunctionModel(_answer)
    resolved = _recording_resolver(lambda _ctx, _id: inner)(None, "test:echo")
    assert isinstance(resolved, UsageRecordingModel) and resolved.wrapped is inner
    inferred = _recording_resolver(lambda _ctx, _id: None)(None, "test")
    assert isinstance(inferred, UsageRecordingModel) and isinstance(inferred.wrapped, TestModel)


def test_event_fields_from_a_stored_row():
    stored = {
        "model_id": "test:priced", "provider_model_name": "priced", "input_tokens": 3, "output_tokens": 4,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": Decimal("0.0001200000"), "price_source": "override",
    }
    assert _usage_fields_from_row(stored) == {
        "model_id": "test:priced",
        "usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_tokens": 0, "cache_write_tokens": 0,
                  "measured": True, "provider_model_name": "priced", "price_source": "override",
                  "recorded_attempt": "earlier"},
        "cost_usd": "0.0001200000",
    }
    assert _same_usage(stored, {**stored, "cost_usd": None, "price_source": "x"})
    assert not _same_usage(stored, {**stored, "output_tokens": 5})


def test_model_message_event_follows_an_earlier_executions_row(worker, tenant):
    """A crash after the in-step write and before the step checkpoint: the re-executed request's
    model_message reports the row that stands, never its own (different) usage."""
    from worker_tests.conftest import event_rows

    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, uuid.uuid4(), 1, "{}", run_id)
    first = workflow.request_usage_input("test:echo", "echo", RequestUsage(input_tokens=10, output_tokens=20))
    rows, _ = workflow._usage_rows("model:0", first)
    workflow._write_request_usage(tenant, str(run_id), rows)
    second = workflow.request_usage_input("test:echo", "echo", RequestUsage(input_tokens=10, output_tokens=99))
    asyncio.run(append_event_step(str(run_id), tenant, "model:0:final", "model_message", {"model_request": 0}, usage=second))
    ((_, key, _, data),) = [e for e in event_rows(worker.engine, run_id) if e[1] == "model:0:final"]
    assert data["usage"]["output_tokens"] == 20 and data["usage"]["recorded_attempt"] == "earlier"
    with worker.engine.connect() as conn:
        from sqlalchemy import text

        out = conn.execute(text("SELECT output_tokens FROM run_model_requests WHERE run_id = :r"), {"r": str(run_id)}).scalars().all()
    assert out == [20]
    # The usual case: the same usage as the stored row, so the event is exactly what it was before.
    run2 = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, uuid.uuid4(), 1, "{}", run2)
    workflow._write_request_usage(tenant, str(run2), rows)
    asyncio.run(append_event_step(str(run2), tenant, "model:0:final", "model_message", {"model_request": 0}, usage=first))
    ((_, _, _, data2),) = [e for e in event_rows(worker.engine, run2) if e[1] == "model:0:final"]
    assert "recorded_attempt" not in data2["usage"] and data2["usage"]["output_tokens"] == 20
