"""The "Bluesky Post" example workflow (`examples/workflows/bluesky-post.json`) and its guide
(`docs/workflows/bluesky-post.md`), through the real API and a real `python -m eap_worker` subprocess.

The worker's HTTP client is replaced by a mock transport that plays the PDS (`new_client` of
`eap_worker.http_tool`, registered by a tiny launcher): no request leaves the machine. The fake PDS logs what
it received to a file, so the tests can see the password reach the wire exactly once per login and nowhere else.

- the spec validates and publishes without warnings; only the secret NAME is in it; the guide quotes it;
- a run completes: login with the stored password, createRecord with `Bearer <accessJwt>`, the record built from
  the input and the clock (`$template`), output `{uri, cid, handle}`;
- a refused login fails `bluesky_auth_failed` before any post, a refused post fails `bluesky_post_failed`, a
  missing secret fails `secret_not_found` with nothing sent;
- the password is in no event, no run row and no step output.
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
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_schemas.workflows import WorkflowSpec
from eap_secrets import KEY_ENV, generate_key

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, requires_pg
from .test_kb_source_workflow_acceptance import SNIPPET, resolve_pointer
from .test_run_e2e import DBOS_URL, REPO_ROOT, _drop_schema

EXAMPLE = REPO_ROOT / "examples" / "workflows" / "bluesky-post.json"
GUIDE = REPO_ROOT / "docs" / "workflows" / "bluesky-post.md"
BASE = "/api/v1/workflows"
API = "/api/v1"
TERMINAL = ("completed", "failed", "needs_attention", "cancelled")
SECRET = "bluesky-app-password"
PASSWORD = "FAKE-app-password-q7x2-not-real"  # obviously fake
HANDLE = "example-account.pds.example.social"
PDS = "https://pds.example.social"

# The worker's entry point with `http.request` talking to a fake PDS (see the module docstring).
WORKER_LAUNCHER = """
import json, os, runpy
import httpx2
import eap_worker.http_tool as http_tool

LOG = os.environ["EAP_TEST_PDS_LOG"]
PASSWORD = os.environ["EAP_TEST_PDS_PASSWORD"]


def handler(request):
    body = json.loads(request.content) if request.content else None
    with open(LOG, "a") as f:
        f.write(json.dumps({"method": request.method, "url": str(request.url), "auth": request.headers.get("authorization"),
                            "body": body}) + "\\n")
    if request.url.host != "pds.example.social":
        return httpx2.Response(404, json={"error": "NotFound"})
    path = request.url.path
    if path == "/xrpc/com.atproto.server.createSession":
        if body.get("identifier") == "example-account.pds.example.social" and body.get("password") == PASSWORD:
            return httpx2.Response(200, json={"accessJwt": "jwt-access-1", "refreshJwt": "jwt-refresh-1",
                                               "handle": "example-account.pds.example.social", "did": "did:plc:fakeaccount",
                                               "email": "me@example.com", "emailConfirmed": True})
        return httpx2.Response(401, json={"error": "AuthenticationRequired", "message": "Invalid identifier or password"})
    if path == "/xrpc/com.atproto.repo.createRecord":
        if request.headers.get("authorization") != "Bearer jwt-access-1":
            return httpx2.Response(401, json={"error": "AuthMissing"})
        if body["record"]["text"].startswith("REFUSE"):
            return httpx2.Response(400, json={"error": "InvalidRequest", "message": "no"})
        return httpx2.Response(200, json={"uri": "at://did:plc:fakeaccount/app.bsky.feed.post/3kfake", "cid": "bafyfakecid",
                                           "commit": {"cid": "bafycommit", "rev": "r1"}, "validationStatus": "valid"})
    return httpx2.Response(404, json={"error": "NotFound"})


http_tool.new_client = lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
runpy.run_module("eap_worker", run_name="__main__")
"""


def load_example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text())


# ------------------------------------------------------------------------------- pure checks


def test_the_example_is_a_valid_spec_with_only_the_secret_name():
    example = load_example()
    assert set(example) == {"description", "agents", "input", "spec"} and example["agents"] == {}
    spec = WorkflowSpec.model_validate(example["spec"])
    assert spec.name == "Bluesky Post"
    tools = [(s.id, s.tool_id) for s in spec.steps if s.type == "tool"]
    assert tools == [("now", "clock.now"), ("session", "http.request"), ("login_failed", "workflow.return"),
                     ("post", "http.request"), ("post_failed", "workflow.return"), ("result", "workflow.return")]
    dumped = json.dumps(example)
    assert f"secret:{SECRET}" in dumped and dumped.count(f"secret:{SECRET}") >= 2  # the steps and the description
    assert PASSWORD not in dumped
    steps = {s.id: s for s in spec.steps}
    assert steps["session"].input["json"] == {"identifier": HANDLE, "password": f"secret:{SECRET}"}
    assert steps["session"].input["url"] == f"{PDS}/xrpc/com.atproto.server.createSession"
    assert steps["post"].input["url"] == f"{PDS}/xrpc/com.atproto.repo.createRecord"
    # No Bluesky-specific code anywhere: it is `http.request` and `$template`.
    assert {s.tool_id for s in spec.steps if s.type == "tool"} == {"clock.now", "http.request", "workflow.return"}


def test_the_input_and_output_schemas():
    spec = load_example()["spec"]
    ok = {"text": "hi", "langs": ["zh-Hant"]}
    jsonschema.validate(ok, spec["input_schema"])
    for bad in ({"text": "", "langs": ["en"]}, {"text": "x" * 301, "langs": ["en"]}, {"text": "hi"}, {"langs": ["en"]},
                {**ok, "extra": 1}, {"text": "hi", "langs": []}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, spec["input_schema"])
    jsonschema.validate({"uri": "at://x", "cid": "c", "handle": "h"}, spec["output_schema"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"uri": "at://x", "cid": "c"}, spec["output_schema"])


def test_the_guide_quotes_the_example_and_every_snippet_matches():
    guide = GUIDE.read_text()
    snippets = list(SNIPPET.finditer(guide))
    assert snippets and len(snippets) == guide.count("<!-- example:")
    for snippet in snippets:
        assert snippet["file"] == "bluesky-post.json"
        assert json.loads(snippet["body"]) == resolve_pointer(load_example(), snippet["pointer"])
    assert "(../../examples/workflows/bluesky-post.json)" in guide
    for word in (SECRET, HANDLE, PDS, "Settings", "secrets.request", "bluesky_auth_failed", "bluesky_post_failed",
                 "langs", "$template", "workflow.return", "accessJwt", "http.request", "change"):
        assert word in guide, word
    assert PASSWORD not in guide


# ------------------------------------------------------------------------------- through the API


class Stack:
    def __init__(self, client: TestClient, engine: Any, tenant: str, pds_log: Path) -> None:
        self.client, self.engine, self.tenant, self.pds_log = client, engine, tenant, pds_log
        self.h = get_auth_header(tenant, "alice")

    def publish(self, spec: dict[str, Any]) -> str:
        created = self.client.post(BASE, json={"spec": spec}, headers=self.h)
        assert created.status_code == 201, created.text
        workflow_id = created.json()["id"]
        published = self.client.post(f"{BASE}/{workflow_id}/versions", headers=self.h)
        assert published.status_code == 201, published.text
        assert published.json()["warnings"] == []
        return workflow_id

    def set_secret(self, value: str | None) -> None:
        if value is None:
            self.client.delete(f"{API}/secrets/{SECRET}", headers=self.h)
            return
        put = self.client.put(f"{API}/secrets/{SECRET}", json={"value": value}, headers=self.h)
        assert put.status_code == 200, put.text

    def start(self, workflow_id: str, input_value: dict[str, Any]) -> str:
        r = self.client.post(f"{BASE}/{workflow_id}/runs", json={"input": input_value}, headers=self.h)
        assert r.status_code == 202, r.text
        return r.json()["run_id"]

    def run(self, workflow_id: str, input_value: dict[str, Any], timeout: float = 90) -> tuple[str, dict[str, Any]]:
        run_id = self.start(workflow_id, input_value)
        deadline = time.monotonic() + timeout
        while True:
            run = self.client.get(f"{API}/runs/{run_id}", headers=self.h).json()
            if run["status"] in TERMINAL:
                return run_id, run
            assert time.monotonic() < deadline, run
            time.sleep(0.25)

    def events(self, run_id: str) -> dict[str, tuple[str, dict[str, Any]]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"), {"r": run_id})
            return {r.event_key: (r.type, r.data) for r in rows}

    def error_code(self, run_id: str) -> str | None:
        end = self.events(run_id).get("end")
        return (end[1] or {}).get("error_code") if end else None

    def everything_recorded(self, run_id: str) -> str:
        with self.engine.connect() as conn:
            row = conn.execute(text("SELECT to_jsonb(r)::text FROM runs r WHERE id = :r"), {"r": run_id}).scalar_one()
            events = conn.execute(text("SELECT data::text FROM execution_events WHERE run_id = :r"), {"r": run_id}).scalars().all()
        return "\n".join([row, *events])

    def sent(self) -> list[dict[str, Any]]:
        if not self.pds_log.exists():
            return []
        return [json.loads(line) for line in self.pds_log.read_text().splitlines() if line]

    def clear(self) -> None:
        self.pds_log.write_text("")


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    """The API in-process and a worker subprocess whose HTTP client is the fake PDS."""
    suffix = uuid.uuid4().hex[:10]
    pds_log = Path(tempfile.mkdtemp()) / "pds.log"
    env = {
        "EAP_DATABASE_URL": PG_URL or "",
        "EAP_DBOS_URL": DBOS_URL,
        "EAP_DBOS_SYSTEM_SCHEMA": f"eap_test_api_{suffix}",
        "EAP_APP_VERSION": f"bsky-{suffix}",
        "EAP_EXECUTOR_ID": f"bsky-{suffix}",
        "EAP_QUEUE_POLL_SECONDS": "0.2",
        "EAP_ALLOWED_MODELS": "test:*",
        "EAP_TEST_PDS_LOG": str(pds_log),
        "EAP_TEST_PDS_PASSWORD": PASSWORD,
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
        app = create_app(database_url=PG_URL)
        engine = app.state.database_engine
        with TestClient(app) as client:
            yield Stack(client, engine, tenant, pds_log)
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
                for table in ("tenant_secrets", "workflow_versions", "workflows"):
                    conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
            engine.dispose()
        _drop_schema(env["EAP_DBOS_SYSTEM_SCHEMA"])
        log.close()
        os.unlink(log.name)


@pytest.fixture(autouse=True)
def _models_for_the_stack(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")


@pytest.fixture(scope="module")
def workflow(stack) -> str:
    return stack.publish(load_example()["spec"])


@requires_pg
def test_a_post_completes_and_the_password_stays_out_of_every_record(stack, workflow):
    stack.set_secret(PASSWORD)
    stack.clear()
    started = datetime.now().astimezone()
    run_id, run = stack.run(workflow, {"text": "你好，Bluesky！", "langs": ["zh-Hant"]})
    assert run["status"] == "completed", run
    output = json.loads(run["output"])
    assert output == {"uri": "at://did:plc:fakeaccount/app.bsky.feed.post/3kfake", "cid": "bafyfakecid", "handle": HANDLE}
    jsonschema.validate(output, load_example()["spec"]["output_schema"])
    login, post = stack.sent()
    assert login["url"] == f"{PDS}/xrpc/com.atproto.server.createSession" and login["auth"] is None
    assert login["body"] == {"identifier": HANDLE, "password": PASSWORD}  # the value reached the wire, once
    assert post["url"] == f"{PDS}/xrpc/com.atproto.repo.createRecord" and post["auth"] == "Bearer jwt-access-1"
    assert post["body"]["repo"] == "did:plc:fakeaccount" and post["body"]["collection"] == "app.bsky.feed.post"
    record = post["body"]["record"]
    assert record["$type"] == "app.bsky.feed.post" and record["text"] == "你好，Bluesky！" and record["langs"] == ["zh-Hant"]
    created = datetime.fromisoformat(record["createdAt"])  # RFC 3339 from clock.now
    assert created.tzinfo is not None and abs((created - started).total_seconds()) < 120
    recorded = stack.everything_recorded(run_id)
    assert PASSWORD not in recorded and "jwt-refresh-1" not in recorded  # dropped by drop_response_paths
    events = stack.events(run_id)
    assert events["step:session:completed"][1]["output"]["json"] == {
        "accessJwt": "jwt-access-1", "handle": HANDLE, "did": "did:plc:fakeaccount"}
    assert events["step:session:started"][1]["input"]["json"]["password"] == f"secret:{SECRET}"  # the reference only
    assert "step:login_failed:started" not in events and "step:post_failed:started" not in events


@requires_pg
def test_a_refused_login_fails_with_bluesky_auth_failed_before_any_post(stack, workflow):
    stack.set_secret("FAKE-a-wrong-password-0000")
    stack.clear()
    run_id, run = stack.run(workflow, {"text": "hello", "langs": ["en"]})
    assert run["status"] == "failed", run
    assert stack.error_code(run_id) == "bluesky_auth_failed"
    events = stack.events(run_id)
    assert events["step:session:completed"][1]["output"]["status"] == 401
    assert events["step:login_failed:failed"][1]["error_code"] == "bluesky_auth_failed"
    assert "step:post:started" not in events
    assert [r["url"].rsplit(".", 1)[-1] for r in stack.sent()] == ["createSession"]
    assert "FAKE-a-wrong-password-0000" not in stack.everything_recorded(run_id)


@requires_pg
def test_a_refused_post_fails_with_bluesky_post_failed(stack, workflow):
    stack.set_secret(PASSWORD)
    stack.clear()
    run_id, run = stack.run(workflow, {"text": "REFUSE this one", "langs": ["en"]})
    assert run["status"] == "failed", run
    assert stack.error_code(run_id) == "bluesky_post_failed"
    events = stack.events(run_id)
    assert events["step:post:completed"][1]["output"]["status"] == 400
    assert events["step:post_failed:failed"][1]["error_code"] == "bluesky_post_failed"
    assert "step:result:started" not in events
    assert PASSWORD not in stack.everything_recorded(run_id)


@requires_pg
def test_a_missing_secret_fails_with_secret_not_found_and_sends_nothing(stack, workflow):
    stack.set_secret(None)
    stack.clear()
    run_id, run = stack.run(workflow, {"text": "hello", "langs": ["en"]})
    assert run["status"] == "failed", run
    assert stack.error_code(run_id) == "secret_not_found"
    assert stack.sent() == []
    assert "step:post:started" not in stack.events(run_id)


@requires_pg
def test_an_input_that_breaks_the_schema_is_refused_before_a_run_starts(stack, workflow):
    for bad in ({"text": "x" * 301, "langs": ["en"]}, {"text": "hello"}, {"langs": ["en"]}):
        r = stack.client.post(f"{BASE}/{workflow}/runs", json={"input": bad}, headers=stack.h)
        assert r.status_code == 422, (bad, r.text)
