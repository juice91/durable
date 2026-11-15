"""AC.18 compatibility: specs and DBOS checkpoints recorded by the build BEFORE the agent capabilities
milestone (commit 8acdda2: main as deployed when the milestone landed), recovered and re-run by this build.

Nothing here is simulated. The previous build is `git archive 8acdda2` in a temp directory with its
own virtualenv (`uv sync --all-packages --frozen`, about 2 s from the uv cache). Each test:

1. creates an app schema migrated BY THE PREVIOUS BUILD to its head (`0013_semantic_recall`), and a
   private DBOS system schema;
2. starts the previous build's API (its own `eap_api`, served by THIS tree's launcher
   `tests/recovery/api_server.py`: see `API_LAUNCHER`) and worker, creates
   and publishes agents and workflows over HTTP, and starts runs and turns with them;
3. stops (or crash-kills, at the previous build's own `EAP_CRASH_AT` points) the previous build,
   upgrades the schema with THIS build's migrations to head (0014_approvals to 0017_api_keys: exactly what a
   deploy does), and starts this build's API and worker with the same application version and
   executor id, so the new worker recovers the old workflows.

| # | What | Proves |
|---|------|--------|
| G1 | every pre-existing spec shape run to completion on the old build, then the SAME published version run on this build: plain, a built-in tool, processors (PII + moderation), knowledge (`retrieve`), and the memory shapes (history, working memory, semantic recall, observational, incl. the observer workflow) | identical DBOS step names and order (the golden list is what the old build recorded), identical event story and output; the stored spec and its hash are unchanged and read back with no new keys |
| C1 | old worker killed `after_tool_call` (ticket reserved, service answered, no checkpoint) | the new worker replays the old checkpoints: one ticket, same story, steps equal to a clean old run |
| C2 | old worker killed mid-stream in model request 1 (`model_request`) | the tool step is not re-run (1 call), the answer completes |
| C3 | old worker killed `after_event_write` on `model:0:final` of a processors run | the stored event keeps its seq and data; one moderation usage row |
| C4 | old worker killed `after_event_write` on `end` of a semantic-recall turn (the turn's answer is stored; `eap.finish_run` and the post-turn `eap.memory_index` never ran) | the new worker recovers through the post-turn steps: one `embed:index:1` event and usage row, one indexed row per role |
| C5 | the same for an observational-memory turn | finish_run and the observer child workflow run once on the new build |
| C6 | old worker killed `workflow_waiting` on a workflow approval step | 0014's backfill puts the open approval in the new inbox; deciding it through this build's route resumes the old workflow, which completes once |
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import APP_DB_URL, DBOS_DB_URL, REPO_ROOT, WorkerProcess, _libpq, read_sse, wait_for

# main as deployed when the agent capabilities milestone merged (RAG P1 included; the first
# baseline was 3369526, the commit before the milestone branched). Still at 0013_semantic_recall.
PREVIOUS_COMMIT = "8acdda2"
PREVIOUS_HEAD = "0013_semantic_recall"
OBSERVER = {"provider": "test", "name": "observer"}
TERMINAL = {"completed", "failed", "needs_attention"}
# Both builds' APIs are launched by THIS tree's harness script, run by each build's own interpreter
# from its own directory, so the app code is still the build's own (`eap_api` comes from its venv).
# The previous build's copy announced `EAP_API_READY port=<n>` with print(), which writes the text
# and its newline separately: the API's access-log line for the /healthz probe (another thread)
# could land in between and glue its timestamp onto the port (`port=511002026`), and connecting to
# that port fails in getaddrinfo with "[Errno 8] nodename nor servname provided". This tree's
# launcher writes the line and its newline in one call.
API_LAUNCHER = REPO_ROOT / "tests" / "recovery" / "api_server.py"


# --------------------------------------------------------------------------- the previous build


@pytest.fixture(scope="session")
def previous_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`git archive` of the previous commit with its own virtualenv; returns its directory."""
    root = tmp_path_factory.mktemp(f"eap-build-{PREVIOUS_COMMIT}")
    archive = subprocess.run(["git", "archive", PREVIOUS_COMMIT], cwd=REPO_ROOT, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout, check=True)
    env = {k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "PYTHONPATH"}}
    env["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
    sync = subprocess.run(["uv", "sync", "--all-packages", "--frozen", "-q"], cwd=root, env=env,
                          capture_output=True, text=True, timeout=600)
    assert sync.returncode == 0, sync.stderr[-4000:]
    probe = subprocess.run([str(root / ".venv/bin/python"), "-I", "-c", "import eap_worker; print(eap_worker.__file__)"],
                           capture_output=True, text=True, check=True)
    assert probe.stdout.strip().startswith(str(root)), probe.stdout  # the old code, not this tree's
    return root


def base_database_url() -> str:
    """eap_app without the eap_db plugin's search_path option (that schema is at THIS build's head)."""
    from sqlalchemy.engine import make_url

    url = make_url(APP_DB_URL)
    return url.difference_update_query(["options"]).render_as_string(hide_password=False)


class BuildProcess(WorkerProcess):
    """A worker or API process of one build: its own interpreter, sources and working directory."""

    def __init__(self, env: dict[str, str], log_path: Path, argv: list[str], cwd: Path, ready: str) -> None:
        self.READY = ready
        self.log_path = log_path
        self._log = log_path.open("w")
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=self._log, stderr=subprocess.STDOUT)

    @property
    def base_url(self) -> str:
        # The whole line, so a glued announcement fails here with the log, not as a bogus port.
        match = re.search(r"^EAP_API_READY port=(\d{1,5})$", self.log(), re.MULTILINE)
        assert match, self.log()[-4000:]
        return f"http://127.0.0.1:{match.group(1)}"


class World:
    """One test's schemas, tenant and processes, shared by the previous build and this one."""

    def __init__(self, previous: Path, tmp_path: Path) -> None:
        suffix = uuid.uuid4().hex[:10]
        self.previous = previous
        self.tmp_path = tmp_path
        self.base_url = base_database_url()
        self.schema = f"eap_compat_{suffix}"
        self.dbos_schema = f"eap_compat_dbos_{suffix}"
        self.tenant = f"tcompat{suffix}"
        self.headers = {"Authorization": f"Bearer dev:{self.tenant}:alice"}
        self.procs: list[BuildProcess] = []
        self.api: BuildProcess | None = None
        self.client: Any = None
        self.build = "previous"
        self.crash_marker = tmp_path / "crash.marker"
        self.executor_id = f"compat-{suffix}"
        self.env = {
            "EAP_AUTH_MODE": "dev",
            "EAP_DBOS_URL": DBOS_DB_URL,
            "EAP_DBOS_SYSTEM_SCHEMA": self.dbos_schema,
            "EAP_APP_VERSION": f"compat-{suffix}",
            "EAP_EXECUTOR_ID": self.executor_id,
            "EAP_QUEUE_POLL_SECONDS": "0.2",
            "EAP_ALLOWED_MODELS": "test:*",
            "EAP_ALLOWED_EMBEDDING_MODELS": "test:*",
            "EAP_APPROVAL_POLL_SECONDS": "1",
            "EAP_WORKFLOW_HOUR_SECONDS": "3600",
            "PYTHONUNBUFFERED": "1",
        }
        # The previous build migrates its own schema to its own head.
        made = self._previous_python(
            "import sys; from eap_db.testing import create_migrated_schema;"
            " print(create_migrated_schema(sys.argv[1], sys.argv[2], public=True))",
            self.base_url, self.schema,
        )
        self.app_url = made.strip().splitlines()[-1]
        from sqlalchemy import create_engine

        self.engine = create_engine(self.app_url, pool_pre_ping=True)
        assert self.revision() == PREVIOUS_HEAD

    # ---- builds

    def _previous_python(self, code: str, *args: str) -> str:
        out = subprocess.run([str(self.previous / ".venv/bin/python"), "-I", "-c", code, *args],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-4000:]
        return out.stdout

    def _process_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        drop = ("EAP_CRASH", "EAP_TEST_", "VIRTUAL_ENV", "PYTHONPATH")
        env = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
        env.update(self.env)
        root = self.previous if self.build == "previous" else REPO_ROOT
        env["EAP_DATABASE_URL"] = self.app_url
        env["EAP_MODEL_PRICES_FILE"] = str(root / "config" / "model-prices.test.json")
        env.update(extra or {})
        return env

    def _python(self) -> str:
        return str(self.previous / ".venv/bin/python") if self.build == "previous" else sys.executable

    def _root(self) -> Path:
        return self.previous if self.build == "previous" else REPO_ROOT

    def start_worker(self, name: str, *, crash_at: str | None = None, event_key: str | None = None) -> BuildProcess:
        extra: dict[str, str] = {}
        if crash_at:
            extra = {"EAP_CRASH_AT": crash_at, "EAP_CRASH_MARKER": str(self.crash_marker)}
            if event_key:
                extra["EAP_CRASH_EVENT_KEY"] = event_key
        proc = BuildProcess(self._process_env(extra), self.tmp_path / f"{self.build}-{name}.log",
                            [self._python(), "-m", "eap_worker"], self._root(), "EAP_WORKER_READY")
        self.procs.append(proc)
        proc.wait_ready()
        return proc

    def start_api(self) -> BuildProcess:
        import httpx2

        api = BuildProcess(self._process_env(), self.tmp_path / f"{self.build}-api.log",
                           [self._python(), str(API_LAUNCHER)], self._root(),
                           "EAP_API_READY")
        self.procs.append(api)
        api.wait_ready()
        if self.client is not None:
            self.client.close()
        self.api, self.client = api, httpx2.Client(base_url=api.base_url, timeout=60.0)
        return api

    def stop_all(self) -> None:
        for proc in self.procs:
            with contextlib.suppress(Exception):
                proc.stop()
        if self.client is not None:
            with contextlib.suppress(Exception):
                self.client.close()
            self.client = None

    def upgrade_to_this_build(self) -> None:
        """Stop the previous build, migrate with THIS build's migrations to head, switch builds."""
        from alembic import command
        from sqlalchemy import create_engine

        from eap_db import alembic_config
        from eap_db.testing import schema_url

        self.stop_all()
        migrator = create_engine(schema_url(self.base_url, self.schema))  # the schema alone, never public
        try:
            with migrator.begin() as conn:
                command.upgrade(alembic_config(conn), "head")
        finally:
            migrator.dispose()
        assert self.revision() == "0017_api_keys"
        self.build = "this"

    def close(self) -> None:
        import psycopg

        self.stop_all()
        self.engine.dispose()
        with psycopg.connect(_libpq(self.base_url), autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        with psycopg.connect(_libpq(DBOS_DB_URL), autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{self.dbos_schema}" CASCADE')

    # ---- API

    def post(self, path: str, body: Any = None, expect: int | tuple[int, ...] = (200, 201, 202)) -> Any:
        r = self.client.post(f"/api/v1{path}", json=body if body is not None else {}, headers=self.headers)
        assert r.status_code in ((expect,) if isinstance(expect, int) else expect), (path, r.status_code, r.text)
        return r.json()

    def get(self, path: str) -> Any:
        r = self.client.get(f"/api/v1{path}", headers=self.headers)
        assert r.status_code == 200, (path, r.text)
        return r.json()

    def published_agent(self, spec: dict[str, Any]) -> str:
        agent_id = self.post("/agents", {"spec": spec}, 201)["id"]
        self.post(f"/agents/{agent_id}/versions", expect=201)
        return agent_id

    def start_run(self, agent_id: str, input_text: str) -> str:
        run_id = str(uuid.uuid4())
        self.post(f"/agents/{agent_id}/runs", {"run_id": run_id, "input": input_text, "version": None}, 201)
        return run_id

    def start_turn(self, conversation_id: str, input_text: str) -> str:
        run_id = str(uuid.uuid4())
        self.post(f"/conversations/{conversation_id}/runs", {"run_id": run_id, "input": input_text}, 201)
        return run_id

    def conversation(self, agent_id: str) -> str:
        return self.post(f"/agents/{agent_id}/conversations", {}, 201)["id"]

    # ---- inspection

    def sql(self, query: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return list(conn.execute(text(query), params))

    def revision(self) -> str:
        return self.sql("SELECT version_num FROM alembic_version")[0][0]

    def events(self, run_id: str) -> list[tuple[int, str, str, dict[str, Any]]]:
        return [tuple(r) for r in self.sql(
            "SELECT seq, event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq", r=run_id)]

    def run_row(self, run_id: str) -> dict[str, Any]:
        (row,) = self.sql("SELECT status, output, error FROM runs WHERE id = :r AND tenant_id = :t", r=run_id, t=self.tenant)
        return dict(row._mapping)

    def _dbos(self, query: str, *args: Any) -> list[tuple[Any, ...]]:
        import psycopg

        with psycopg.connect(_libpq(DBOS_DB_URL), connect_timeout=5) as conn:
            if conn.execute("SELECT to_regclass(%s)", (f'"{self.dbos_schema}".workflow_status',)).fetchone()[0] is None:
                return []
            return [tuple(r) for r in conn.execute(query.replace("{s}", f'"{self.dbos_schema}"'), args)]

    def workflow_status(self, workflow_id: str) -> list[str]:
        return [r[0] for r in self._dbos("SELECT status FROM {s}.workflow_status WHERE workflow_uuid = %s", workflow_id)]

    def steps(self, workflow_id: str) -> list[str]:
        return [r[0] for r in self._dbos(
            "SELECT function_name FROM {s}.operation_outputs WHERE workflow_uuid = %s ORDER BY function_id", workflow_id)]

    def wait_workflow(self, workflow_id: str, timeout: float = 90) -> None:
        try:
            wait_for(lambda: self.workflow_status(workflow_id) == ["SUCCESS"], timeout=timeout,
                     what=f"workflow {workflow_id} SUCCESS")
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n{self.logs()}") from None

    def wait_crash(self, proc: BuildProcess, crash_at: str) -> None:
        assert proc.wait_exit() == 137, proc.log()[-4000:]
        assert f"EAP_CRASH_AT={crash_at} reached" in proc.log()

    def logs(self) -> str:
        return "\n".join(f"--- {p.log_path.name}\n{p.log()[-3000:]}" for p in self.procs)


@pytest.fixture
def world(previous_build: Path, tmp_path: Path) -> Iterator[World]:
    w = World(previous_build, tmp_path)
    try:
        yield w
    finally:
        w.close()


_ID = re.compile(r"^(tool|embed:retrieve|memory:wm):[^:]+(:|$)")


# The previous build (8acdda2) already has main's P1.C1: a completed conversation turn of a spec
# with `memory.semantic_recall` and/or `memory.observational` ends with its post-turn marker
# (`memory:post_turn:{turn}`, `post_turn_done`) on both builds, so the layouts and stories compare
# as recorded. (Against the first baseline, 3369526, this build's extra marker step was added to
# the old build's lists before comparing.)


def story(events: list[tuple[int, str, str, dict[str, Any]]]) -> list[tuple[str, str]]:
    """(event_key with tool call ids normalised, type), in seq order."""
    return [(_ID.sub(lambda m: f"{m.group(1)}:<id>{m.group(2)}", key), typ) for _seq, key, typ, _ in events]


def assert_gap_free(events: list[tuple[int, str, str, dict[str, Any]]]) -> None:
    seqs = [e[0] for e in events]
    assert seqs == list(range(len(seqs))), seqs
    assert len({e[1] for e in events}) == len(events), [e[1] for e in events]


# --------------------------------------------------------------------------- G1: golden step lists

NEW_SPEC_KEYS = {"output", "approvals", "limits", "fallback_models"}
PII = {"type": "pii_redact", "config": {"kinds": ["email"], "action": "redact"}}
MODERATION = {"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}}
WM_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "city": {"type": "string"}}}


def spec(model: str, *, tools: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    return {"name": f"Compat {model}", "instructions": "Answer the user.",
            "model": {"provider": "test", "name": model}, "tools": list(tools), **extra}


def kb_with_document(w: World) -> str:
    kb_id = w.post("/knowledge-bases", {"name": f"kb-{uuid.uuid4().hex[:8]}", "embedding_model": "test:hash"}, 201)["id"]
    r = w.client.put(
        f"/api/v1/knowledge-bases/{kb_id}/documents/printer",
        json={"title": "Printer", "content": "# Printer\n\n## Toner\n\nWhen the toner light blinks, replace the"
              " cartridge.\n\n## Paper\n\nLoad A4 paper face down.\n", "mime_type": "text/markdown"},
        headers=w.headers,
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    wait_for(lambda: w.run_row(run_id)["status"] in TERMINAL, timeout=60, what="ingest")
    assert w.run_row(run_id)["status"] == "completed", w.logs()
    return kb_id


def shapes(kb_id: str) -> dict[str, tuple[dict[str, Any], str, bool]]:
    """name -> (spec, input, is a conversation turn). Every spec shape the previous build ran."""
    return {
        "plain": (spec("echo"), "hi", False),
        "tool": (spec("ticket", tools=("tickets.create",)), "Laptop broken", False),
        "processors": (spec("echo", input_processors=[PII], output_processors=[PII, MODERATION]),
                       "mail ada@example.com", False),
        "knowledge": (spec("retrieve", knowledge_bases=[{"id": kb_id}]), "toner light", False),
        "memory_history": (spec("echo", memory={"history": {"last_turns": 5}}), "hi", True),
        "memory_working": (spec("memory", memory={"working": {"json_schema": WM_SCHEMA}}), "remember name=Ana", True),
        "memory_recall": (spec("echo", memory={"semantic_recall": {"embedding_model": "test:hash"}}), "hi", True),
        "memory_observational": (spec("echo", memory={"observational": {
            "model": OBSERVER, "observe_after_tokens": 1000, "keep_recent_turns": 2,
            "reflect_after_tokens": 200_000}}), "hi", True),
    }


def ticketless(output: str | None) -> str | None:
    return re.sub(r"TCK-[0-9A-F]+", "TCK-<id>", output) if output else output


def run_shape(w: World, agent_id: str, input_text: str, turn: bool) -> dict[str, Any]:
    run_id = w.start_turn(w.conversation(agent_id), input_text) if turn else w.start_run(agent_id, input_text)
    w.wait_workflow(run_id)  # post-turn steps included
    observer = f"{run_id}:memory-observe"
    if w.workflow_status(observer):
        w.wait_workflow(observer)
    events = w.events(run_id)
    assert_gap_free(events)
    return {"run_id": run_id, "row": w.run_row(run_id), "steps": w.steps(run_id),
            "observer_steps": w.steps(observer), "story": story(events)}


def test_g1_every_pre_milestone_spec_shape_keeps_its_steps_story_and_hash(world: World, report) -> None:
    w = world
    w.start_api()
    w.start_worker("worker")
    kb_id = kb_with_document(w)
    agents = {name: w.published_agent(s) for name, (s, _i, _t) in shapes(kb_id).items()}
    old = {name: run_shape(w, agents[name], i, t) for name, (_s, i, t) in shapes(kb_id).items()}
    stored = {
        a: tuple(w.sql("SELECT spec, spec_hash FROM agent_versions WHERE agent_id = :a AND version = 1", a=a)[0])
        for a in agents.values()}
    for name, got in old.items():
        assert got["row"]["status"] == "completed", (name, got["row"], w.logs())

    w.upgrade_to_this_build()
    # Memory is per user (the note, the recall index): start the second pass from the same empty
    # memory as the first, so each turn sees the same inputs.
    with w.engine.begin() as conn:
        from sqlalchemy import text

        for table in ("working_memories", "message_embeddings", "memory_observations"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": w.tenant})
    w.start_api()
    w.start_worker("worker")
    for name, (agent_spec, input_text, turn) in shapes(kb_id).items():
        new = run_shape(w, agents[name], input_text, turn)
        before = old[name]
        assert new["row"]["status"] == "completed", (name, new["row"], w.logs())
        steps, events = before["steps"], before["story"]
        assert new["steps"] == steps, (name, steps, new["steps"])
        assert new["observer_steps"] == before["observer_steps"], name
        assert new["story"] == events, (name, events, new["story"])
        assert ticketless(new["row"]["output"]) == ticketless(before["row"]["output"]), name
        # The old run reads back through this build's API, as text (no output_json).
        body = w.get(f"/runs/{before['run_id']}")
        assert body["status"] == "completed" and body.get("output_json") is None, body
        report(f"AC.18 G1 {name}: {len(new['steps'])} steps + {len(new['observer_steps'])} observer steps,"
               f" {len(new['story'])} events, equal to the previous build's ({new['steps'][-1]})")
    for agent_id, (spec_json, spec_hash) in stored.items():
        (now,) = w.sql("SELECT spec, spec_hash FROM agent_versions WHERE agent_id = :a AND version = 1", a=agent_id)
        assert (now.spec, now.spec_hash) == (spec_json, spec_hash)
        version = w.get(f"/agents/{agent_id}/versions/1")
        assert not NEW_SPEC_KEYS & set(version["spec"]), version["spec"]


# --------------------------------------------------------------------------- C1-C5: in-flight agent runs


def reset_memory(w: World) -> None:
    from sqlalchemy import text

    with w.engine.begin() as conn:
        for table in ("working_memories", "message_embeddings", "memory_observations"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": w.tenant})


def usage_keys(w: World, run_id: str) -> list[str]:
    keys = [r[0] for r in w.sql("SELECT request_key FROM run_model_requests WHERE run_id = :r ORDER BY request_key",
                                r=run_id)]
    assert len(keys) == len(set(keys)), keys  # one usage row per request key
    return [_ID.sub(lambda m: f"{m.group(1)}:<id>{m.group(2)}", k) for k in keys]


def tickets(w: World, run_id: str) -> tuple[int, int]:
    made = w.sql("SELECT count(*) FROM fake_external_tickets WHERE idempotency_key LIKE :p", p=f"{run_id}:%")[0][0]
    calls = w.sql("SELECT count(*) FROM fake_external_ticket_calls WHERE idempotency_key LIKE :p", p=f"{run_id}:%")[0][0]
    return made, calls


MEMORY_RECALL = {"semantic_recall": {"embedding_model": "test:hash"}}
MEMORY_OBSERVE = {"observational": {"model": OBSERVER, "observe_after_tokens": 1000, "keep_recent_turns": 2,
                                    "reflect_after_tokens": 200_000}}
IN_FLIGHT = {
    # name: (spec, input, turn, crash point, event key, run status at the kill)
    "C1-after-tool-call": (spec("ticket", tools=("tickets.create",)), "Laptop broken", False,
                           "after_tool_call", None, "running"),
    "C2-mid-stream": (spec("ticket", tools=("tickets.create",)), "Laptop broken", False,
                      "model_request", None, "running"),
    "C3-processors-event": (spec("echo", input_processors=[PII], output_processors=[PII, MODERATION]),
                            "mail ada@example.com", False, "after_event_write", "model:0:final", "running"),
    "C4-recall-before-post-turn": (spec("echo", memory=MEMORY_RECALL), "the code word is pelican", True,
                                   "after_event_write", "end", "running"),
    "C5-observe-before-post-turn": (spec("echo", memory=MEMORY_OBSERVE), "hi", True,
                                    "after_event_write", "end", "running"),
}


@pytest.mark.parametrize("name", list(IN_FLIGHT))
def test_in_flight_run_of_the_previous_build_recovers_on_this_build(world: World, report, name: str) -> None:
    w = world
    agent_spec, input_text, turn, crash_at, event_key, status_at_kill = IN_FLIGHT[name]
    w.start_api()
    healthy = w.start_worker("reference")
    agent_id = w.published_agent(agent_spec)
    reference = run_shape(w, agent_id, input_text, turn)
    assert reference["row"]["status"] == "completed", reference["row"]
    reference_usage = usage_keys(w, reference["run_id"])
    healthy.stop()
    reset_memory(w)

    crashing = w.start_worker("crashing", crash_at=crash_at, event_key=event_key)
    run_id = w.start_turn(w.conversation(agent_id), input_text) if turn else w.start_run(agent_id, input_text)
    w.wait_crash(crashing, crash_at)
    assert w.run_row(run_id)["status"] == status_at_kill
    before = w.events(run_id)
    if event_key:
        assert before[-1][1] == event_key, [e[1] for e in before]

    w.upgrade_to_this_build()
    w.start_api()
    recovering = w.start_worker("recovering")
    w.wait_workflow(run_id)
    observer = f"{run_id}:memory-observe"
    if reference["observer_steps"]:
        w.wait_workflow(observer)
    assert "Recovering" in recovering.log(), recovering.log()[-3000:]

    events = w.events(run_id)
    assert_gap_free(events)
    assert events[: len(before)] == before, "an event the previous build stored changed on recovery"
    # The recovered run's layout and story equal a clean run of the previous build.
    ref_steps, ref_story = reference["steps"], reference["story"]
    assert story(events) == ref_story, (story(events), ref_story)
    assert w.steps(run_id) == ref_steps, (w.steps(run_id), ref_steps)
    assert w.steps(observer) == reference["observer_steps"]
    assert w.workflow_status(run_id) == ["SUCCESS"]
    row = w.run_row(run_id)
    assert row["status"] == "completed", (row, w.logs())
    assert ticketless(row["output"]) == ticketless(reference["row"]["output"])
    assert usage_keys(w, run_id) == reference_usage
    if "tickets.create" in agent_spec["tools"]:
        made, calls = tickets(w, run_id)
        assert made == 1 and calls == (2 if crash_at == "after_tool_call" else 1), (made, calls)
    if name.startswith("C4"):
        roles = w.sql("SELECT turn, role FROM message_embeddings WHERE tenant_id = :t AND run_id = :r ORDER BY role",
                      t=w.tenant, r=run_id)
        assert [(r.turn, r.role) for r in roles] == [(1, "assistant"), (1, "user")]
    # This build's API serves the recovered run: status, totals and an SSE replay equal to the DB.
    body = w.get(f"/runs/{run_id}")
    assert body["status"] == "completed" and body.get("output_json") is None
    frames = read_sse(w.client, f"/api/v1/runs/{run_id}/stream?after_seq=-1", w.headers)
    replay = [(f.data["seq"], f.data["type"]) for f in frames if f.event == "run_event"]
    terminal = next(seq for seq, _k, typ, _d in events if typ == "run_completed")
    assert replay == [(seq, typ) for seq, _k, typ, _d in events if seq <= terminal]
    report(f"AC.18 {name}: killed the previous build at {crash_at}; this build recovered with"
           f" {len(events)} events and {len(reference['steps'])} steps, equal to a clean previous-build run")


# --------------------------------------------------------------------------- C6: an open workflow approval


def approval_workflow(agent_id: str) -> dict[str, Any]:
    return {
        "name": "Compat approval",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "start": "approve",
        "steps": [
            {"id": "approve", "type": "approval", "prompt": "Ship it?", "timeout_hours": 1, "next": "done"},
            {"id": "done", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}},
        ],
    }


WAIT_ROUND = {"eap.workflow_clock", "DBOS.recv", "DBOS.sleep"}


def without_wait_rounds(steps: list[str]) -> list[str]:
    out: list[str] = []
    for name in steps:
        if name in WAIT_ROUND or (name == "eap.workflow_approval_decision" and out and out[-1] == name):
            continue
        out.append(name)
    return out


def start_workflow_run(w: World, workflow_id: str) -> str:
    run_id = str(uuid.uuid4())
    w.post(f"/workflows/{workflow_id}/runs", {"run_id": run_id, "input": {"q": "printer on fire"}}, 202)
    return run_id


def test_c6_open_workflow_approval_is_backfilled_and_decided_on_this_build(world: World, report) -> None:
    w = world
    w.start_api()
    healthy = w.start_worker("reference")
    agent_id = w.published_agent(spec("echo"))
    workflow_id = w.post("/workflows", {"spec": approval_workflow(agent_id)}, 201)["id"]
    w.post(f"/workflows/{workflow_id}/versions", expect=201)
    reference = start_workflow_run(w, workflow_id)
    wait_for(lambda: any(e[1] == "step:approve:waiting" for e in w.events(reference)), timeout=60, what="waiting")
    w.post(f"/workflows/runs/{reference}/steps/approve/approve", {"comment": "go"}, 200)
    w.wait_workflow(reference)
    healthy.stop()

    crashing = w.start_worker("crashing", crash_at="workflow_waiting", event_key="step:approve:waiting")
    run_id = start_workflow_run(w, workflow_id)
    w.wait_crash(crashing, "workflow_waiting")
    before = w.events(run_id)
    assert before[-1][1] == "step:approve:waiting"

    w.upgrade_to_this_build()
    # 0014's backfill: the open approval the previous build recorded is in this build's inbox.
    (row,) = w.sql("SELECT kind, approval_key, prompt, decision FROM pending_approvals WHERE run_id = :r", r=run_id)
    assert (row.kind, row.approval_key, row.prompt, row.decision) == ("step", "approve", "Ship it?", None)
    w.start_api()
    recovering = w.start_worker("recovering")
    inbox = w.get("/approvals?kind=step")["items"]
    assert [(i["run_id"], i["step_id"], i["prompt"]) for i in inbox] == [(run_id, "approve", "Ship it?")], inbox
    decided = w.post(f"/workflows/runs/{run_id}/steps/approve/approve", {"comment": "go"}, 200)
    assert (decided["decision"], decided["decided_by"]) == ("approved", "alice")
    w.wait_workflow(run_id)
    assert "Recovering" in recovering.log(), recovering.log()[-3000:]
    events = w.events(run_id)
    assert_gap_free(events)
    assert events[: len(before)] == before
    assert story(events) == story(w.events(reference))
    # The approval wait polls (decision check, clock, recv) a number of times that depends on when the
    # decision arrived; everything around the wait is the same.
    assert without_wait_rounds(w.steps(run_id)) == without_wait_rounds(w.steps(reference)), (
        w.steps(run_id), w.steps(reference))
    assert w.run_row(run_id)["status"] == "completed"
    assert w.run_row(run_id)["output"] == w.run_row(reference)["output"]
    assert w.get("/approvals?kind=step")["items"] == []  # decided: off the pending inbox
    again = w.client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/reject", json={}, headers=w.headers)
    assert again.status_code == 409, again.text
    report(f"AC.18 C6: the previous build's open approval was backfilled, decided on this build; {len(events)} events")
