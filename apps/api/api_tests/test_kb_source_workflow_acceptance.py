"""The "KB Source Synchronization" example workflow (`examples/workflows/kb-source-sync.json`) and its
guide (`docs/workflows/kb-source-sync.md`), through the real API and a real `python -m eap_worker`
subprocess whose catalogue is the fake adapter (registered by a tiny launcher: the adapter is
(product_catalog, fake); parts named `BAD...` make the sync fail with `provider_error`, parts named
`SLOW...` make a segment take 8 s, and a connection whose config carries `skip_check` passes its check
with whatever secrets it has).

- the spec validates and publishes without warnings; the guide quotes it;
- a targeted sync completes and the run output equals the sync report, valid under `output_schema` and
  far under the 32 KiB step cap; an incremental run without `part_numbers` (the engine applies no schema default) takes the `sync_all` path;
- a failed child sync fails the parent `output_invalid` (the report tool answers `sync_failed`), a child
  that needs attention (`secret_missing`) does too (`sync_needs_attention`), a failing connection check
  stops before any sync, and a sync that outlasts the wait loop fails with `loop_max_iterations`;
- a workflow schedule with input `{source_id, mode: "incremental"}` fires through `eap.schedule_tick`
  and its run starts a sync.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_kb_sources.fake import FakeCatalogAdapter
from eap_kb_sources.registry import register_adapter
from eap_schemas.workflows import WorkflowSpec
from eap_secrets import KEY_ENV, generate_key

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, requires_pg
from .test_run_e2e import DBOS_URL, REPO_ROOT, _drop_schema

EXAMPLE = REPO_ROOT / "examples" / "workflows" / "kb-source-sync.json"
GUIDE = REPO_ROOT / "docs" / "workflows" / "kb-source-sync.md"
BASE = "/api/v1/workflows"
API = "/api/v1"
TERMINAL = ("completed", "failed", "needs_attention", "cancelled")
SNIPPET = re.compile(r"<!-- example: (?P<file>\S+) (?P<pointer>\S+) -->\n```json\n(?P<body>.*?)\n```", re.DOTALL)
STEP_OUTPUT_CAP = 32 * 1024

# The worker's entry point with the fake catalogue registered (see the module docstring).
WORKER_LAUNCHER = """
import asyncio, runpy
from eap_kb_sources.errors import SourceError
from eap_kb_sources.fake import FakeCatalogAdapter
from eap_kb_sources.registry import register_adapter
from eap_kb_sources.adapter import ConnectionCheck

class Adapter(FakeCatalogAdapter):
    def __init__(self):
        super().__init__(size=12, page_size=5)

    async def check_connection(self, connection, secrets):
        if connection.config.get("skip_check"):
            return ConnectionCheck(True)
        return await super().check_connection(connection, secrets)

    async def sync_segment(self, request, connection, secrets, store):
        if any(p.startswith("SLOW") for p in request.part_numbers):
            await asyncio.sleep(8)
        if any(p.startswith("BAD") for p in request.part_numbers):
            raise SourceError("provider_error", "the fake provider failed")
        return await super().sync_segment(request, connection, secrets, store)

register_adapter("product_catalog", "fake", Adapter)
runpy.run_module("eap_worker", run_name="__main__")
"""


def load_example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text())


def resolve_pointer(doc: Any, pointer: str) -> Any:
    assert pointer.startswith("/"), pointer
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        doc = doc[int(token)] if isinstance(doc, list) else doc[token]
    return doc


# ------------------------------------------------------------------------------- pure checks


def test_the_example_is_a_valid_spec_without_agents():
    example = load_example()
    assert set(example) == {"description", "agents", "input", "spec"} and example["agents"] == {}
    spec = WorkflowSpec.model_validate(example["spec"])
    assert spec.name == "KB Source Synchronization"
    tools = [s.tool_id for s in spec.steps if s.type == "tool"]
    assert tools == ["kb.source.get", "settings.connection.check", "settings.connection.check",
                     "kb.source.sync", "workflow.run.result", "kb.source.sync", "workflow.run.result"]
    loops = [s for s in spec.steps if s.type == "loop"]
    assert [(s.id, s.max_iterations, s.on_max_iterations) for s in loops] == [
        ("wait_targeted", 100, "fail"), ("wait_all", 100, "fail")]
    assert example["spec"]["input_schema"]["properties"]["part_numbers"]["default"] == []
    assert "ti." not in json.dumps(example["spec"]).replace("mi.", "")  # no provider identifier


def test_the_output_schema_allows_the_report_keys_and_nothing_else():
    schema = load_example()["spec"]["output_schema"]
    base = {"run_id": "r", "status": "completed", "source_id": "s", "mode": "full", "discovered": 1, "inserted": 1,
            "updated": 0, "unchanged": 0, "documents_queued": 0, "failed": 0, "checkpoint": {}, "errors": []}
    jsonschema.validate(base, schema)
    jsonschema.validate({**base, "removed": 2, "budget": {"date": "2026-11-18", "used": 3, "remaining": 9},
                         "source_revision": None, "complete": True, "stop_reason": None}, schema)
    jsonschema.validate({**base, "source_revision": "r1"}, schema)
    for bad in ({**base, "extra": 1}, {**base, "status": "failed"}, {**base, "errors": [0] * 51}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


def test_the_guide_quotes_the_example_and_every_snippet_matches():
    guide = GUIDE.read_text()
    snippets = list(SNIPPET.finditer(guide))
    assert snippets and len(snippets) == guide.count("<!-- example:")
    for snippet in snippets:
        assert snippet["file"] == "kb-source-sync.json"
        assert json.loads(snippet["body"]) == resolve_pointer(load_example(), snippet["pointer"])
    assert "(../../examples/workflows/kb-source-sync.json)" in guide
    for word in ("part_numbers", "incremental", "schedule", "output_invalid", "loop_max_iterations", "cancel"):
        assert word in guide


# ------------------------------------------------------------------------------- through the API


class Stack:
    def __init__(self, client: TestClient, engine: Any, tenant: str) -> None:
        self.client, self.engine, self.tenant = client, engine, tenant
        self.h = get_auth_header(tenant, "alice")
        self.workflow_id = ""

    def publish(self, spec: dict[str, Any]) -> str:
        created = self.client.post(BASE, json={"spec": spec}, headers=self.h)
        assert created.status_code == 201, created.text
        workflow_id = created.json()["id"]
        published = self.client.post(f"{BASE}/{workflow_id}/versions", headers=self.h)
        assert published.status_code == 201, published.text
        assert published.json()["warnings"] == []
        return workflow_id

    def source(self, *, refs: dict[str, str] | None = None, skip_check: bool = True) -> str:
        """A knowledge base, a fake connection and a source of it: the source id."""
        kb = self.client.post(f"{API}/knowledge-bases", json={"name": f"kb-{uuid.uuid4().hex[:6]}",
                              "embedding_model": "test:hash"}, headers=self.h)
        assert kb.status_code == 201, kb.text
        conn = self.client.post(f"{API}/settings/connections", json={
            "name": f"c-{uuid.uuid4().hex[:6]}", "kind": "oauth2_client_credentials",
            "base_url": "https://catalogue.example.com",
            "config": {"provider": "fake", **({"skip_check": True} if skip_check else {})},
            "credential_refs": refs or {}}, headers=self.h)
        assert conn.status_code == 201, conn.text
        src = self.client.post(f"{API}/knowledge-bases/{kb.json()['id']}/sources", json={
            "connection_id": conn.json()["id"], "name": "parts", "kind": "product_catalog", "provider": "fake",
            "config": {"document_policy": {"types": ["datasheet"]}}}, headers=self.h)
        assert src.status_code == 201, src.text
        return src.json()["id"]

    def start(self, workflow_id: str, input_value: dict[str, Any]) -> str:
        r = self.client.post(f"{BASE}/{workflow_id}/runs", json={"input": input_value}, headers=self.h)
        assert r.status_code == 202, r.text
        return r.json()["run_id"]

    def wait(self, run_id: str, timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            run = self.client.get(f"{API}/runs/{run_id}", headers=self.h).json()
            if run["status"] in TERMINAL:
                return run
            assert time.monotonic() < deadline, run
            time.sleep(0.25)

    def run(self, workflow_id: str, input_value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        run_id = self.start(workflow_id, input_value)
        return run_id, self.wait(run_id)

    def events(self, run_id: str) -> dict[str, tuple[str, dict[str, Any]]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"),
                {"r": run_id},
            )
            return {r.event_key: (r.type, r.data) for r in rows}

    def error_code(self, run_id: str) -> str | None:
        end = self.events(run_id).get("end")
        return (end[1] or {}).get("error_code") if end else None

    def children(self, parent_run_id: str) -> list[dict[str, Any]]:
        """The sync runs the parent started: (run_id, status, source_id, error_code)."""
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT s.run_id, r.status, s.status AS sync_status FROM kb_source_syncs s"
                " JOIN runs r ON r.tenant_id = s.tenant_id AND r.id::text = s.run_id"
                " WHERE s.tenant_id = :t AND s.parent_run_id = :p"), {"t": self.tenant, "p": parent_run_id}).all()
        return [{"run_id": r.run_id, "status": r.status, "sync_status": r.sync_status} for r in rows]

    def wait_children_done(self, parent_run_id: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(c["status"] in TERMINAL for c in self.children(parent_run_id)):
                return
            time.sleep(0.5)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    """The API in-process and a worker subprocess (the fake catalogue registered, a 2 s schedule tick)."""
    suffix = uuid.uuid4().hex[:10]
    env = {
        "EAP_DATABASE_URL": PG_URL or "",
        "EAP_DBOS_URL": DBOS_URL,
        "EAP_DBOS_SYSTEM_SCHEMA": f"eap_test_api_{suffix}",
        "EAP_APP_VERSION": f"kbs-{suffix}",
        "EAP_EXECUTOR_ID": f"kbs-{suffix}",
        "EAP_QUEUE_POLL_SECONDS": "0.2",
        "EAP_SCHEDULE_TICK": "*/2 * * * * *",
        "EAP_ALLOWED_MODELS": "test:*",
        "EAP_ALLOWED_EMBEDDING_MODELS": "test:*",
        KEY_ENV: generate_key(),
    }
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)  # noqa: SIM115
    proc = subprocess.Popen([sys.executable, "-c", WORKER_LAUNCHER], cwd=REPO_ROOT, env={**os.environ, **env},
                            stdout=log, stderr=subprocess.STDOUT, text=True)
    patch = pytest.MonkeyPatch()
    engine, tenant = None, f"t{uuid.uuid4().hex[:10]}"
    try:
        deadline = time.monotonic() + 60
        while "EAP_WORKER_READY" not in Path(log.name).read_text():
            assert proc.poll() is None and time.monotonic() < deadline, Path(log.name).read_text()[-3000:]
            time.sleep(0.3)
        for k, v in env.items():
            patch.setenv(k, v)
        patch.setenv("EAP_AUTH_MODE", "dev")
        register_adapter("product_catalog", "fake", FakeCatalogAdapter)  # the API validates a source's adapter too
        app = create_app(database_url=PG_URL)
        engine = app.state.database_engine
        with TestClient(app) as client:
            yield Stack(client, engine, tenant)
    finally:
        patch.undo()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if engine is not None:
            cleanup_tenant(engine, tenant)
            with engine.begin() as conn:
                for table in ("workflow_schedules", "knowledge_bases", "settings_connections", "tenant_secrets",
                              "workflow_versions", "workflows"):
                    conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
            engine.dispose()
        _drop_schema(env["EAP_DBOS_SYSTEM_SCHEMA"])
        log.close()
        os.unlink(log.name)


@pytest.fixture(autouse=True)
def _models_for_the_stack(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")


@pytest.fixture(scope="module")
def workflow(stack) -> str:
    return stack.publish(load_example()["spec"])


@requires_pg
def test_a_targeted_sync_completes_and_the_output_is_the_bounded_report(stack, workflow):
    source = stack.source()
    run_id, run = stack.run(workflow, {"source_id": source, "mode": "targeted", "part_numbers": ["FAKE0001", "FAKE0002"]})
    assert run["status"] == "completed", run
    output = json.loads(run["output"])
    jsonschema.validate(output, load_example()["spec"]["output_schema"])
    assert len(run["output"].encode()) < STEP_OUTPUT_CAP
    assert (output["status"], output["mode"], output["source_id"]) == ("completed", "targeted", source)
    assert (output["discovered"], output["documents_queued"], output["failed"], output["errors"]) == (2, 2, 0, [])
    events = stack.events(run_id)
    assert events["step:check:completed"][1]["output"]["ok"] is True
    assert "step:connection_failed:started" not in events and "step:sync_all:started" not in events
    [child] = stack.children(run_id)
    assert child["run_id"] == output["run_id"] and child["status"] == "completed"
    assert events["step:sync_targeted:completed"][1]["output"] == {"run_id": child["run_id"], "status": "queued"}


@requires_pg
def test_an_incremental_run_without_part_numbers_takes_the_sync_all_path(stack, workflow):
    source = stack.source()
    run_id, run = stack.run(workflow, {"source_id": source, "mode": "incremental"})
    assert run["status"] == "completed", run
    output = json.loads(run["output"])
    assert (output["mode"], output["discovered"]) == ("incremental", 12)
    assert "step:sync_all:completed" in stack.events(run_id) and "step:sync_targeted:started" not in stack.events(run_id)
    jsonschema.validate(output, load_example()["spec"]["output_schema"])


@requires_pg
def test_a_failed_child_sync_fails_the_parent_with_a_stable_code(stack, workflow):
    source = stack.source()
    run_id, run = stack.run(workflow, {"source_id": source, "mode": "targeted", "part_numbers": ["BAD0001"]})
    assert run["status"] == "failed", run
    assert stack.error_code(run_id) == "output_invalid"
    events = stack.events(run_id)
    assert events["step:report_targeted:completed"][1]["output"]["error"]["code"] == "sync_failed"
    [child] = stack.children(run_id)
    assert child["status"] == "failed"


@requires_pg
def test_a_child_that_needs_attention_fails_the_parent_and_says_so(stack, workflow):
    source = stack.source(refs={"client_secret": "secret:never-stored"})
    run_id, run = stack.run(workflow, {"source_id": source, "mode": "targeted", "part_numbers": ["FAKE0001"]})
    assert run["status"] == "failed", run
    assert stack.error_code(run_id) == "output_invalid"
    assert stack.events(run_id)["step:report_targeted:completed"][1]["output"]["error"]["code"] == "sync_needs_attention"
    [child] = stack.children(run_id)
    assert (child["status"], child["sync_status"]) == ("needs_attention", "needs_attention")
    with stack.engine.connect() as conn:
        end = conn.execute(text("SELECT data FROM execution_events WHERE run_id = :r AND event_key = 'end'"),
                           {"r": child["run_id"]}).scalar_one()
    assert end["error_code"] == "secret_missing"


@requires_pg
def test_a_failing_connection_check_stops_before_any_sync(stack, workflow):
    source = stack.source(refs={"client_secret": "secret:never-stored"}, skip_check=False)
    run_id, run = stack.run(workflow, {"source_id": source, "mode": "targeted", "part_numbers": ["FAKE0001"]})
    assert run["status"] == "failed" and stack.error_code(run_id) == "output_invalid"
    events = stack.events(run_id)
    assert events["step:check:completed"][1]["output"]["ok"] is False
    assert events["step:connection_failed:completed"][1]["output"]["error_code"] == "secret_missing"
    assert "step:sync_targeted:started" not in events and stack.children(run_id) == []


@requires_pg
def test_a_sync_that_outlasts_the_wait_loop_fails_with_a_stable_timeout_code(stack):
    spec = json.loads(EXAMPLE.read_text())["spec"]
    spec["name"] = "KB Source Synchronization (short wait)"
    for loop in (s for s in spec["steps"] if s["type"] == "loop"):
        loop["max_iterations"] = 2
        loop["body"]["steps"][0]["input"]["wait_seconds"] = 1
    source = stack.source()
    run_id, run = stack.run(stack.publish(spec), {"source_id": source, "mode": "targeted", "part_numbers": ["SLOW0001"]})
    assert run["status"] == "failed", run
    assert stack.error_code(run_id) == "loop_max_iterations"
    assert "step:report_targeted:started" not in stack.events(run_id)
    stack.wait_children_done(run_id)


@requires_pg
def test_a_workflow_schedule_fires_an_incremental_sync(stack, workflow):
    source = stack.source()
    created = stack.client.post(f"{BASE}/{workflow}/schedules", json={
        "name": "nightly", "cron": "0 3 * * *", "timezone": "UTC",
        "input": {"source_id": source, "mode": "incremental"}}, headers=stack.h)
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]
    with stack.engine.begin() as conn:  # make it due now
        conn.execute(text("UPDATE workflow_schedules SET next_fire_at = now() - interval '1 minute' WHERE id = :s"),
                     {"s": schedule_id})
    deadline, run_id = time.monotonic() + 60, None
    while run_id is None:
        with stack.engine.connect() as conn:
            run_id = conn.execute(text("SELECT id FROM runs WHERE schedule_id = :s"), {"s": schedule_id}).scalar()
        assert time.monotonic() < deadline, "the schedule never fired"
        time.sleep(0.5)
    run = stack.wait(str(run_id))
    assert run["status"] == "completed", run
    output = json.loads(run["output"])
    assert (output["mode"], output["status"]) == ("incremental", "completed")
    [child] = stack.children(str(run_id))
    assert child["run_id"] == output["run_id"] and child["status"] == "completed"


@requires_pg
def test_cancelling_the_parent_run_cancels_the_child_sync(stack, workflow):
    source = stack.source()
    run_id = stack.start(workflow, {"source_id": source, "mode": "targeted", "part_numbers": ["SLOW0001"]})
    deadline = time.monotonic() + 60
    while not stack.children(run_id):  # the sync is started and its segment sleeps 8 s
        assert time.monotonic() < deadline, "the sync was never started"
        time.sleep(0.2)
    [child] = stack.children(run_id)
    started = time.monotonic()
    r = stack.client.post(f"{API}/runs/{run_id}/cancel", headers=stack.h)
    assert r.status_code == 202, r.text
    run = stack.wait(run_id, timeout=10)
    assert run["status"] == "failed" and stack.error_code(run_id) == "run_cancelled"
    assert time.monotonic() - started < 6, "the cancel waited for the slow segment"
    events = stack.events(child["run_id"])
    assert events["cancel"][0] == "cancel_requested"
    assert events["end"][1]["error_code"] == "run_cancelled"
    assert stack.client.get(f"{API}/runs/{child['run_id']}", headers=stack.h).json()["status"] == "failed"
    time.sleep(9)  # the cancelled segment finishes in the background: the child stays cancelled
    assert stack.client.get(f"{API}/runs/{child['run_id']}", headers=stack.h).json()["status"] == "failed"
    assert stack.events(child["run_id"])["end"][1]["error_code"] == "run_cancelled"
