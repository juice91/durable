"""RunService, SpanService, TraceService, MetricsService and LogService with fake stores."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from eap_api.ports import RunWaitTimeout
from eap_api.services import runs as runs_module
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.logs import LogService, QuerySlots
from eap_api.services.metrics import MetricsService
from eap_api.services.runs import RunService, launch_recorded_run
from eap_api.services.spans import SpanService
from eap_api.services.traces import TraceService
from eap_run_store import ENQUEUE_FAILED_ERROR, MetricsQueryTimeout, RunIdConflict

TENANT = "t1"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def stored_run(**over):
    run = {
        "id": uuid.uuid4(), "kind": "agent", "agent_id": str(uuid.uuid4()), "agent_version": 1, "status": "queued",
        "display_status": "queued", "input": "hi", "output": None, "error": None, "heartbeat_at": None,
        "created_at": NOW, "updated_at": NOW, "conversation_id": None, "draft_spec": None, "trace_id": None,
        "traceparent": None,
    }
    return {**run, **over}


def service(run_store=None, launcher=None):
    return RunService(MagicMock(), run_store or MagicMock(), MagicMock(), launcher or MagicMock())


def test_read_run_json_is_the_api_shape_and_refuses_an_unknown_run():
    store = MagicMock()
    store.get_run.return_value = None
    with pytest.raises(NotFound):
        service(store).read_run_json(TENANT, uuid.uuid4())
    run = stored_run()
    store.get_run.return_value = run
    store.chat_usage.return_value = {}
    launcher = MagicMock()
    launcher.recovery_attempts.return_value = {str(run["id"]): 3}
    body = service(store, launcher).read_run_json(TENANT, run["id"])
    assert body["run_id"] == str(run["id"]) and body["recovered"] is True and body["restarts"] == 2


def test_wait_for_run_returns_the_terminal_row_and_cancels_the_tree_on_timeout():
    store = MagicMock()
    done = stored_run(status="completed")
    store.get_run.side_effect = [stored_run(), done]
    assert service(store).wait_for_run(TENANT, done["id"], timeout=5, poll_interval=0) is done
    store.get_run.side_effect = None
    store.get_run.return_value = stored_run()
    rid = uuid.uuid4()
    with pytest.raises(RunWaitTimeout):
        service(store).wait_for_run(TENANT, rid, timeout=0, poll_interval=0)
    store.cancel_run_tree.assert_called_once_with(TENANT, rid)


def test_a_failed_hand_off_is_unavailable_and_marks_an_owned_run_failed(monkeypatch):
    monkeypatch.setattr(runs_module, "version_row", lambda *a: type("V", (), {"spec": {"model": 1}})())
    store = MagicMock()
    launcher = MagicMock()
    launcher.launch.side_effect = RuntimeError("dbos down")
    run = stored_run()
    with pytest.raises(Unavailable):
        launch_recorded_run(MagicMock(), launcher, store, TENANT, run, True)
    store.mark_enqueue_failed.assert_called_once_with(TENANT, run["id"])


def test_a_superseded_turn_whose_enqueue_failed_is_a_conflict():
    store = MagicMock()
    store.requeue_after_enqueue_failure.return_value = False
    store.get_run.return_value = stored_run(status="failed", error=ENQUEUE_FAILED_ERROR, conversation_id="c")
    with pytest.raises(Conflict):
        launch_recorded_run(MagicMock(), MagicMock(), store, TENANT, stored_run(conversation_id="c"), False)


def test_start_run_conflicts(monkeypatch):
    agent = MagicMock(archived=False)
    monkeypatch.setattr(runs_module, "lock_agent", lambda *a: agent)
    monkeypatch.setattr(runs_module, "resolve_run_options", lambda *a, **k: MagicMock(store_kwargs=lambda: {}))
    store = MagicMock()
    store.get_run.return_value = None
    store.create_run.side_effect = RunIdConflict("x")
    with pytest.raises(Conflict) as exc:
        service(store).start_run(TENANT, uuid.uuid4(), run_id=uuid.uuid4(), input_text="x")
    assert exc.value.message == "run_id is already in use"
    # a recorded run asked for again with another input
    store.get_run.return_value = stored_run()
    with pytest.raises(Conflict) as exc:
        service(store).start_run(TENANT, uuid.uuid4(), run_id=uuid.uuid4(), input_text="other")
    assert exc.value.message == "run_id is already used by a different run request"
    agent.archived = True
    store.get_run.return_value = None
    with pytest.raises(Conflict):
        service(store).start_run(TENANT, uuid.uuid4(), run_id=uuid.uuid4(), input_text="x")


def test_list_runs_filter_refusals_and_timeout():
    blank = dict(
        limit=10, before=None, agent_id=None, run_status=None, kind=None, tool_id=None, workflow_id=None,
        knowledge_base_id=None, parent_run_id=None, created_from=None, created_to=None, model=None, provider=None,
        tags=None, metadata=None, q=None,
    )
    svc = service()
    with pytest.raises(InvalidInput, match="cursor"):
        svc.list_runs(TENANT, **{**blank, "before": "garbage"})
    with pytest.raises(InvalidInput, match="status"):
        svc.list_runs(TENANT, **{**blank, "run_status": [str(i) for i in range(11)]})
    with pytest.raises(InvalidInput, match="key=value"):
        svc.list_runs(TENANT, **{**blank, "metadata": ["novalue"]})
    with pytest.raises(InvalidInput, match="from must be before to"):
        svc.list_runs(TENANT, **{**blank, "created_from": NOW, "created_to": NOW})
    svc.run_store.list_runs.side_effect = MetricsQueryTimeout(5000)
    with pytest.raises(Unavailable):
        svc.list_runs(TENANT, **blank)
    svc.run_store.list_runs.side_effect = None
    svc.run_store.list_runs.return_value = []
    svc.run_store.chat_usage.return_value = {}
    assert svc.list_runs(TENANT, **blank) == {"items": [], "next_before": None}


def test_histogram_range_rules():
    blank = dict(
        agent_id=None, run_status=None, kind=None, tool_id=None, workflow_id=None, knowledge_base_id=None,
        parent_run_id=None, model=None, provider=None, tags=None, metadata=None, q=None,
    )
    svc = SpanService(MagicMock(), MagicMock())
    with pytest.raises(InvalidInput, match="at most 366 days"):
        svc.runs_histogram(TENANT, created_from=NOW - timedelta(days=400), created_to=NOW, **blank)
    svc.spans.runs_histogram.return_value = [{"start": NOW, "runs": 1, "failed": 0}]
    out = svc.runs_histogram(TENANT, created_from=NOW - timedelta(hours=5), created_to=NOW, **blank)
    assert out["interval"] == "hour" and out["buckets"][0]["start"] == NOW.isoformat()


def test_external_trace_is_not_found_and_links_the_tenants_run():
    svc = SpanService(MagicMock(), MagicMock())
    svc.spans.read_external_trace.return_value = None
    with pytest.raises(NotFound):
        svc.read_external_trace(TENANT, "a" * 32)
    rid = uuid.uuid4()
    svc.spans.read_external_trace.return_value = {
        "trace_id": "a" * 32, "service_name": "s", "resource_attributes": {}, "truncated": False,
        "spans": [{"span_id": "1", "start": NOW, "end": None, "attributes": {"pydanticos.run_id": str(rid)}}],
    }
    svc.run_store.get_run.return_value = {"id": rid}
    assert svc.read_external_trace(TENANT, "a" * 32)["run_id"] == str(rid)
    svc.run_store.get_run.return_value = None
    assert svc.read_external_trace(TENANT, "a" * 32)["run_id"] is None


def test_feedback_requires_the_trace_and_a_span_of_it(monkeypatch):
    svc = TraceService(MagicMock(), MagicMock(), MagicMock())
    svc.run_store.get_run.return_value = None
    with pytest.raises(NotFound):
        svc.add_feedback(TENANT, "u", uuid.uuid4(), rating="up", comment=None, score=None, span_id=None)
    with pytest.raises(NotFound):
        svc.list_feedback(TENANT, uuid.uuid4(), None)
    svc.run_store.get_run.return_value = {"id": 1}
    monkeypatch.setattr(svc, "read_trace", lambda *a: {"spans": [{"id": "s1"}]})
    with pytest.raises(InvalidInput):
        svc.add_feedback(TENANT, "u", uuid.uuid4(), rating="up", comment=None, score=None, span_id="s2")
    svc.feedback.add.return_value.as_json.return_value = {"ok": True}
    assert svc.add_feedback(TENANT, "u", uuid.uuid4(), rating="up", comment="  ", score=None, span_id="s1") == {"ok": True}
    assert svc.feedback.add.call_args.kwargs["comment"] is None


def test_metrics_range_and_timeout():
    svc = MetricsService(MagicMock(), MagicMock(), MagicMock())
    kw = dict(agent_id=None, model=None, provider=None, kind=None, operation=None)
    with pytest.raises(InvalidInput, match="1970"):
        svc.build_query(TENANT, datetime(1969, 1, 1), None, **kw)
    q = svc.build_query(TENANT, None, None, **kw)
    assert q.end - q.start == timedelta(days=7)
    svc.store.timeseries.side_effect = MetricsQueryTimeout(5000)
    with pytest.raises(Unavailable):
        svc.timeseries(q, "hour", None)


def test_log_search_refusals():
    run_store = MagicMock()
    run_store.get_run.return_value = None
    svc = LogService(run_store, MagicMock())
    kw = dict(source=None, slots=QuerySlots(1), run_id=None, agent_id=None, q=None, level=None, from_=None, to=None, limit=10)
    with pytest.raises(NotFound):
        svc.search(TENANT, **{**kw, "run_id": uuid.uuid4()})
    with pytest.raises(InvalidInput, match="1970"):
        svc.search(TENANT, **{**kw, "from_": datetime(1969, 1, 1)})
    with pytest.raises(Unavailable, match="not configured"):
        svc.search(TENANT, **kw)
