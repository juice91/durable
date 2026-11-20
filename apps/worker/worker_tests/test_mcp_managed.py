"""Managed MCP endpoints (local runtime, Task 6): resolved by identity, started on demand, stopped idle.

Hermetic: SQLite carries the three tables the worker touches (the shape of migration 0024), a fake
`McpRuntime` stands in for the operator, and the MCP server is the local FastMCP one on 127.0.0.1.
Nothing here needs a cluster or the shared Postgres."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anyio
import pytest
from eap_mcp_runtime import RuntimeHandle, RuntimeSpec, RuntimeStatus
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from eap_schemas import McpToolRef
from eap_secrets import KEY_ENV, OLD_KEYS_ENV, generate_key, load_keyring, mcp_env_context, mcp_header_context
from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV, OutboundPolicy
from eap_worker import mcp as worker_mcp
from eap_worker import mcp_runtime_sweep as sweep
from eap_worker.mcp import McpToolUnavailable, McpToolset, ensure_managed_running, load_servers, set_mcp_runtime
from worker_tests.mcp_helpers import LocalMcpServer

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)

DDL = [
    "CREATE TABLE mcp_servers (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, url TEXT,"
    " enabled BOOLEAN NOT NULL DEFAULT 1, runtime TEXT NOT NULL DEFAULT 'external',"
    " keep_warm BOOLEAN NOT NULL DEFAULT 0)",
    "CREATE TABLE mcp_server_secrets (tenant_id TEXT NOT NULL, server_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'header',"
    " header_name TEXT NOT NULL, ciphertext BLOB NOT NULL, key_id TEXT NOT NULL,"
    " PRIMARY KEY (tenant_id, server_id, kind, header_name))",
    "CREATE TABLE mcp_runtimes (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, server_id TEXT NOT NULL, catalog_name TEXT,"
    " package TEXT NOT NULL, image TEXT NOT NULL, namespace TEXT NOT NULL, cr_name TEXT NOT NULL,"
    " state TEXT NOT NULL DEFAULT 'pending', endpoint TEXT, last_error TEXT, last_used_at TIMESTAMP,"
    " created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (tenant_id, server_id))",
]


@dataclass
class FakeRuntime:
    """An `McpRuntime` double. `statuses` are returned by successive status() calls (the last repeats)."""

    url: str = "http://mcp-x-proxy.mcp-t.svc:8080/mcp"
    statuses: list[str] = field(default_factory=lambda: ["running"])
    calls: list[tuple[str, object]] = field(default_factory=list)
    start_error: Exception | None = None

    async def start(self, spec: RuntimeSpec) -> RuntimeHandle:
        self.calls.append(("start", spec))
        if self.start_error:
            raise self.start_error
        return RuntimeHandle("mcp-t", "mcp-x")

    async def stop(self, handle: RuntimeHandle) -> None:
        self.calls.append(("stop", handle))

    async def delete(self, handle: RuntimeHandle) -> None:
        self.calls.append(("delete", handle))

    async def status(self, handle: RuntimeHandle) -> RuntimeStatus:
        self.calls.append(("status", handle))
        state = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return RuntimeStatus(state, self.url)  # type: ignore[arg-type]

    async def logs(self, handle: RuntimeHandle, tail: int = 200) -> list[str]:
        return []

    def endpoint(self, handle: RuntimeHandle) -> str:
        return self.url

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        for ddl in DDL:
            conn.execute(text(ddl))
    return eng


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.delenv(OLD_KEYS_ENV, raising=False)
    monkeypatch.delenv(DEV_ALLOW_HOSTS_ENV, raising=False)
    monkeypatch.setenv("EAP_MCP_START_POLL_SECONDS", "0.01")
    yield
    set_mcp_runtime(None)


def add_managed(engine, tenant, name="memory", *, state="running", used_ago=None, updated_ago=None, env_secrets=None,
                package=None):
    server_id, runtime_id = str(uuid.uuid4()), str(uuid.uuid4())
    used = None if used_ago is None else NOW - used_ago
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO mcp_servers (id, tenant_id, name, url, runtime) VALUES (:i, :t, :n, NULL, 'managed')"),
                     {"i": server_id, "t": tenant, "n": name})
        conn.execute(
            text("INSERT INTO mcp_runtimes (id, tenant_id, server_id, package, image, namespace, cr_name, state, last_used_at,"
                 " created_at, updated_at) VALUES (:i, :t, :s, :p, 'reg/node:1', 'mcp-t', 'mcp-x', :st, :u, :c, :c2)"),
            {"i": runtime_id, "t": tenant, "s": server_id, "st": state, "u": used,
             "p": package or '{"env": {"MODE": "plain"}, "transport": "streamable-http", "port": 8080}',
             "c": NOW - (updated_ago or timedelta(0)), "c2": NOW - (updated_ago or timedelta(0))},
        )
        ring = load_keyring()
        for var, value in (env_secrets or {}).items():
            sealed = ring.encrypt(value, context=mcp_env_context(tenant, server_id, var))
            conn.execute(text("INSERT INTO mcp_server_secrets (tenant_id, server_id, kind, header_name, ciphertext, key_id)"
                              " VALUES (:t, :s, 'env', :h, :c, :k)"),
                         {"t": tenant, "s": server_id, "h": var, "c": sealed.ciphertext, "k": sealed.key_id})
    return server_id, runtime_id


def state_of(engine, runtime_id):
    with engine.connect() as conn:
        return conn.execute(text("SELECT state, last_error FROM mcp_runtimes WHERE id = :i"), {"i": runtime_id}).one()


def refs(name="memory", tool="read_graph"):
    return [McpToolRef(server=name, tool=tool)]


# --------------------------------------------------------------------------- resolution by identity


def test_worker_resolves_a_managed_endpoint_by_identity(engine):
    fake = FakeRuntime()
    server_id, runtime_id = add_managed(engine, "t1", env_secrets={"API_TOKEN": "env-secret-value"})
    with engine.begin() as conn:  # a header secret next to the env secret: only the header is a header
        sealed = load_keyring().encrypt("Bearer h", context=mcp_header_context("t1", server_id, "Authorization"))
        conn.execute(text("INSERT INTO mcp_server_secrets (tenant_id, server_id, kind, header_name, ciphertext, key_id)"
                          " VALUES ('t1', :s, 'header', 'Authorization', :c, :k)"),
                     {"s": server_id, "c": sealed.ciphertext, "k": sealed.key_id})
    conn = load_servers(engine, "t1", refs(), runtime=fake)["memory"]
    assert conn.url == fake.url
    assert conn.runtime_id == runtime_id and conn.server_id == server_id
    assert conn.headers == {"Authorization": "Bearer h"}  # the env secret is not decrypted as a header


def test_the_managed_client_may_reach_only_its_own_endpoint(engine, monkeypatch):
    monkeypatch.setenv(DEV_ALLOW_HOSTS_ENV, "other.svc")  # dev allowlist is not applied to managed rows
    fake = FakeRuntime()
    add_managed(engine, "t1")
    conn = load_servers(engine, "t1", refs(), runtime=fake)["memory"]
    worker_mcp._client(conn, OutboundPolicy.from_env(), 5)  # builds: its own URL is allowed
    policy = OutboundPolicy.from_env().for_managed_runtime(conn.runtime_id, conn.url)
    with pytest.raises(Exception):
        policy.check_url("http://other.svc:8080/mcp")


def test_worker_cannot_use_another_tenants_runtime(engine):
    fake = FakeRuntime()
    add_managed(engine, "tenant-a")
    # Tenant B names A's server: it does not exist for B.
    with pytest.raises(McpToolUnavailable) as exc:
        load_servers(engine, "tenant-b", refs(), runtime=fake)
    assert exc.value.code == "server_not_found"
    # A forged runtime row from another tenant under B's server row is not B's runtime either.
    server_b, runtime_b = add_managed(engine, "tenant-b", name="mine")
    with engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET tenant_id = 'tenant-a' WHERE id = :i"), {"i": runtime_b})
    with pytest.raises(McpToolUnavailable) as exc:
        load_servers(engine, "tenant-b", refs("mine"), runtime=fake)
    assert exc.value.code == "runtime_missing"
    # ... and a start for A's runtime never touches B's rows or objects.
    assert fake.calls == []


def test_a_disabled_managed_server_is_unavailable(engine):
    add_managed(engine, "t1")
    with engine.begin() as conn:
        conn.execute(text("UPDATE mcp_servers SET enabled = 0"))
    with pytest.raises(McpToolUnavailable) as exc:
        load_servers(engine, "t1", refs(), runtime=FakeRuntime())
    assert exc.value.code == "server_disabled"


# --------------------------------------------------------------------------- start on demand


def conn_for(engine, fake, tenant="t1"):
    return load_servers(engine, tenant, refs(), runtime=fake)["memory"]


def test_a_stopped_runtime_is_started_and_waited_for(engine):
    fake = FakeRuntime(statuses=["starting", "starting", "running"])
    server_id, rid = add_managed(engine, "t1", state="stopped", env_secrets={"API_TOKEN": "env-secret-value"})
    asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "read_graph", runtime=fake))
    assert fake.kinds().count("start") == 1 and fake.kinds()[0] == "start"
    spec = next(v for k, v in fake.calls if k == "start")
    assert (spec.tenant_id, spec.server_id, spec.name, spec.image) == ("t1", server_id, "memory", "reg/node:1")
    assert spec.env == {"MODE": "plain"} and spec.secret_refs == {"API_TOKEN": "env-secret-value"}
    assert (spec.transport, spec.port) == ("streamable-http", 8080)
    assert state_of(engine, rid).state == "running"
    with engine.connect() as c:
        assert c.execute(text("SELECT last_used_at FROM mcp_runtimes WHERE id = :i"), {"i": rid}).scalar() is not None


def test_tool_call_during_cold_start_waits(engine):
    """State `starting` (another caller started it): no second start; poll until running."""
    fake = FakeRuntime(statuses=["starting", "starting", "starting", "running"])
    _, rid = add_managed(engine, "t1", state="starting", updated_ago=timedelta(seconds=5))
    asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "read_graph", runtime=fake))
    assert "start" not in fake.kinds()
    assert fake.kinds().count("status") == 4
    assert state_of(engine, rid).state == "running"


def test_a_cold_start_that_never_finishes_times_out_and_leaves_the_row_starting(engine, monkeypatch):
    monkeypatch.setenv("EAP_MCP_START_TIMEOUT_SECONDS", "1")
    fake = FakeRuntime(statuses=["starting"])
    _, rid = add_managed(engine, "t1", state="starting")
    with pytest.raises(McpToolUnavailable) as exc:
        asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "read_graph", runtime=fake))
    assert exc.value.code == "runtime_start_timeout"
    assert state_of(engine, rid).state == "starting"  # the sweep fails it after ten minutes


def test_a_failed_start_marks_the_row_failed_without_exception_text(engine):
    fake = FakeRuntime(start_error=RuntimeError("boom with token=abc123"))
    _, rid = add_managed(engine, "t1", state="stopped")
    with pytest.raises(McpToolUnavailable) as exc:
        asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "read_graph", runtime=fake))
    assert exc.value.code == "runtime_start_failed"
    row = state_of(engine, rid)
    assert row.state == "failed" and "abc123" not in (row.last_error or "") and row.last_error == "start_failed: RuntimeError"


def test_a_runtime_that_reports_failed_while_waiting_fails_the_call(engine):
    fake = FakeRuntime(statuses=["starting", "failed"])
    _, rid = add_managed(engine, "t1", state="starting")
    with pytest.raises(McpToolUnavailable) as exc:
        asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "read_graph", runtime=fake))
    assert exc.value.code == "runtime_failed"
    assert state_of(engine, rid).state == "failed"


def test_a_running_runtime_is_only_touched(engine):
    fake = FakeRuntime()
    _, rid = add_managed(engine, "t1", state="running", used_ago=timedelta(minutes=3))
    asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "read_graph", runtime=fake))
    assert fake.calls == []
    with engine.connect() as c:  # touched: no longer the stored value
        used = c.execute(text("SELECT last_used_at FROM mcp_runtimes WHERE id = :i"), {"i": rid}).scalar()
    assert str(used) != str(NOW - timedelta(minutes=3))


def test_concurrent_callers_start_the_runtime_once(tmp_path):
    # a file database: the shared in-memory connection is not safe for callers running in threads
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}", connect_args={"timeout": 30})
    with engine.begin() as conn:
        for ddl in DDL:
            conn.execute(text(ddl))
    fake = FakeRuntime(statuses=["starting", "running"])
    add_managed(engine, "t1", state="stopped")

    async def go():
        conn = conn_for(engine, fake)
        await asyncio.gather(*(ensure_managed_running(engine, "t1", conn, "read_graph", runtime=fake) for _ in range(4)))

    asyncio.run(go())
    assert fake.kinds().count("start") == 1


# --------------------------------------------------------------------------- idle sweep


def test_idle_sweep_stops_and_next_call_restarts(engine):
    fake = FakeRuntime()
    _, idle = add_managed(engine, "t1", "idle", state="running", used_ago=timedelta(minutes=45))
    _, busy = add_managed(engine, "t1", "busy", state="running", used_ago=timedelta(minutes=5))
    _, never = add_managed(engine, "t1", "never", state="running", used_ago=None, updated_ago=timedelta(minutes=40))
    _, other = add_managed(engine, "t1", "off", state="stopped", used_ago=timedelta(hours=9))
    result = asyncio.run(sweep.sweep_runtimes(engine, fake, now=NOW, idle_minutes=30))
    assert result == {"stopped": 2, "failed_starting": 0, "stop_failed": 0}
    assert [k for k in fake.kinds()] == ["stop", "stop"]
    assert state_of(engine, idle).state == "stopped" and state_of(engine, never).state == "stopped"
    assert state_of(engine, busy).state == "running" and state_of(engine, other).state == "stopped"
    # the next call starts it again, transparently
    fake.calls.clear()
    asyncio.run(ensure_managed_running(engine, "t1", load_servers(engine, "t1", refs("idle", "x"), runtime=fake)["idle"],
                                       "x", runtime=fake))
    assert fake.kinds()[0] == "start" and state_of(engine, idle).state == "running"


def test_idle_sweep_skips_a_keep_warm_server(engine):
    fake = FakeRuntime()
    _, warm = add_managed(engine, "t1", "warm", state="running", used_ago=timedelta(hours=5))
    _, cold = add_managed(engine, "t1", "cold", state="running", used_ago=timedelta(hours=5))
    with engine.begin() as conn:
        conn.execute(text("UPDATE mcp_servers SET keep_warm = 1 WHERE name = 'warm'"))
    result = asyncio.run(sweep.sweep_runtimes(engine, fake, now=NOW, idle_minutes=30))
    assert result["stopped"] == 1 and fake.kinds() == ["stop"]
    assert state_of(engine, warm).state == "running" and state_of(engine, cold).state == "stopped"


def test_sweep_fails_starting_rows_older_than_ten_minutes(engine):
    fake = FakeRuntime()
    _, old = add_managed(engine, "t1", "old", state="starting", updated_ago=timedelta(minutes=11))
    _, fresh = add_managed(engine, "t1", "fresh", state="starting", updated_ago=timedelta(minutes=9))
    result = asyncio.run(sweep.sweep_runtimes(engine, fake, now=NOW, idle_minutes=30))
    assert result["failed_starting"] == 1
    row = state_of(engine, old)
    assert row.state == "failed" and row.last_error == "start_timed_out"
    assert state_of(engine, fresh).state == "starting"


def test_a_stop_that_raises_marks_the_row_failed_and_the_sweep_continues(engine):
    class Broken(FakeRuntime):
        async def stop(self, handle):
            raise RuntimeError("api down")

    _, a = add_managed(engine, "t1", "a", state="running", used_ago=timedelta(minutes=99))
    result = asyncio.run(sweep.sweep_runtimes(engine, Broken(), now=NOW, idle_minutes=30))
    assert result["stop_failed"] == 1
    row = state_of(engine, a)
    assert row.state == "failed" and row.last_error == "stop_failed: RuntimeError"


def test_idle_minutes_come_from_the_environment(monkeypatch):
    assert sweep.idle_minutes() == 30
    monkeypatch.setenv("EAP_MCP_IDLE_MINUTES", "5")
    assert sweep.idle_minutes() == 5
    monkeypatch.setenv("EAP_MCP_IDLE_MINUTES", "nonsense")
    assert sweep.idle_minutes() == 30


# --------------------------------------------------------------------------- through the toolset, real MCP server


def test_discovery_and_call_start_a_stopped_server_on_demand(engine):
    ctx = SimpleNamespace(deps=SimpleNamespace(tenant_id="t1", run_id="r1"), tool_call_id="c1")
    with LocalMcpServer() as server:
        fake = FakeRuntime(url=server.url, statuses=["starting", "running"])
        _, rid = add_managed(engine, "t1", "orders", state="stopped")
        set_mcp_runtime(fake)
        toolset = worker_mcp.mcp_toolset_for_run(engine, "t1", [McpToolRef(server="orders", tool="lookup_order")])
        tools = asyncio.run(toolset.get_tools(ctx))
        assert list(tools) == ["mcp__orders__lookup_order"]
        assert fake.kinds().count("start") == 1 and state_of(engine, rid).state == "running"
        # idle sweep stops it; the next call (not discovery) starts it again and goes through
        asyncio.run(sweep.sweep_runtimes(engine, fake, now=datetime.now(timezone.utc) + timedelta(hours=2), idle_minutes=30))
        assert state_of(engine, rid).state == "stopped"
        fake.statuses[:] = ["running"]
        conn = load_servers(engine, "t1", [McpToolRef(server="orders", tool="lookup_order")], runtime=fake)["orders"]
        result = asyncio.run(toolset._invoke(conn, McpToolRef(server="orders", tool="lookup_order"),
                                             {"order_id": "7"}, "k", ctx))
        assert result["is_error"] is False
        assert fake.kinds().count("start") == 2 and server.call_count("lookup_order") == 1


def test_architect_discovery_starts_a_managed_server_and_lists_its_tools(engine, caplog):
    from eap_worker.architect_allowlist import discover_mcp_tools

    with LocalMcpServer() as server:  # loopback stands in for the private in-cluster Service: no dev allowlist
        fake = FakeRuntime(url=server.url, statuses=["running"])
        _, rid = add_managed(engine, "t1", "orders", state="stopped")
        set_mcp_runtime(fake)
        found = asyncio.run(discover_mcp_tools(engine, "t1", ["orders"]))
        assert found == {"orders": ("cancel_order", "lookup_order")}
        assert fake.kinds().count("start") == 1 and state_of(engine, rid).state == "running"
        # already running: no second start
        assert asyncio.run(discover_mcp_tools(engine, "t1", ["orders"])) == found
        assert fake.kinds().count("start") == 1


def test_architect_discovery_of_an_unreachable_managed_server_is_empty_with_a_warning(engine, caplog, monkeypatch):
    from eap_worker import architect_allowlist
    from eap_worker.architect_allowlist import discover_mcp_tools

    monkeypatch.setattr(architect_allowlist, "MCP_DISCOVERY_TIMEOUT_S", 0.5)
    fake = FakeRuntime(statuses=["starting"])  # never becomes running: bounded by the discovery timeout
    add_managed(engine, "t1", "orders", state="stopped")
    set_mcp_runtime(fake)
    with caplog.at_level("WARNING", logger="eap_worker.architect_allowlist"):
        assert asyncio.run(discover_mcp_tools(engine, "t1", ["orders"])) == {"orders": ()}
    assert "mcp discovery failed server=orders" in caplog.text


# --------------------------------------------------------------------------- races (Task 6b)


def _last_used(engine, rid):
    with engine.connect() as c:
        return c.execute(text("SELECT last_used_at FROM mcp_runtimes WHERE id = :i"), {"i": rid}).scalar()


def test_claiming_a_start_refreshes_last_used_so_the_sweep_cannot_take_it_back(engine):
    fake = FakeRuntime(statuses=["running"])
    _, rid = add_managed(engine, "t1", state="stopped", used_ago=timedelta(hours=5))
    asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "read_graph", runtime=fake))
    assert state_of(engine, rid).state == "running"
    assert asyncio.run(sweep.sweep_runtimes(engine, FakeRuntime(), idle_minutes=30))["stopped"] == 0
    assert state_of(engine, rid).state == "running"


def test_a_runtime_stopped_between_read_and_touch_is_started_again(engine):
    fake = FakeRuntime(statuses=["running"])
    _, rid = add_managed(engine, "t1", state="running", used_ago=timedelta(hours=5))
    conn = conn_for(engine, fake)
    real = worker_mcp._TOUCH
    flipped = []

    orig_exec = worker_mcp._exec_returning

    def flip_then_touch(eng, stmt, **params):
        if stmt is real and not flipped:  # the sweep wins just before the touch
            flipped.append(1)
            with eng.begin() as c:
                c.execute(text("UPDATE mcp_runtimes SET state = 'stopped' WHERE id = :i"), {"i": rid})
        return orig_exec(eng, stmt, **params)

    worker_mcp._exec_returning, restore = flip_then_touch, orig_exec
    try:
        asyncio.run(ensure_managed_running(engine, "t1", conn, "read_graph", runtime=fake))
    finally:
        worker_mcp._exec_returning = restore
    assert fake.kinds().count("start") == 1 and state_of(engine, rid).state == "running"


def test_the_sweep_marks_stopping_until_stop_returns(engine):
    seen = []

    class Slow(FakeRuntime):
        async def stop(self, handle):
            seen.append(state_of(engine, rid).state)

    _, rid = add_managed(engine, "t1", state="running", used_ago=timedelta(minutes=99))
    asyncio.run(sweep.sweep_runtimes(engine, Slow(), now=NOW, idle_minutes=30))
    assert seen == ["stopping"] and state_of(engine, rid).state == "stopped"


def test_a_caller_waits_for_stopping_and_starts_only_after_the_stop(engine):
    order = []

    class Ordered(FakeRuntime):
        async def stop(self, handle):
            order.append("stop")
            await asyncio.sleep(0.15)
            order.append("stop-done")

        async def start(self, spec):
            order.append("start")
            return await super().start(spec)

    fake = Ordered()
    _, rid = add_managed(engine, "t1", state="running", used_ago=timedelta(minutes=99))

    async def go():
        conn = conn_for(engine, fake)
        sweeping = asyncio.create_task(sweep.sweep_runtimes(engine, fake, now=NOW, idle_minutes=30))
        await asyncio.sleep(0.05)
        await ensure_managed_running(engine, "t1", conn, "read_graph", runtime=fake)
        await sweeping

    asyncio.run(go())
    assert order == ["stop", "stop-done", "start"] and state_of(engine, rid).state == "running"


def test_a_stuck_stopping_row_fails_after_ten_minutes(engine):
    _, rid = add_managed(engine, "t1", state="stopping", updated_ago=timedelta(minutes=11))
    asyncio.run(sweep.sweep_runtimes(engine, FakeRuntime(), now=NOW, idle_minutes=30))
    row = state_of(engine, rid)
    assert row.state == "failed" and row.last_error == "stop_timed_out"


def test_a_stop_failure_leaves_failed_not_stopping(engine):
    class Broken(FakeRuntime):
        async def stop(self, handle):
            raise RuntimeError("x")

    _, rid = add_managed(engine, "t1", state="running", used_ago=timedelta(minutes=99))
    asyncio.run(sweep.sweep_runtimes(engine, Broken(), now=NOW, idle_minutes=30))
    assert state_of(engine, rid).state == "failed"


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_non_finite_timeouts_fall_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("EAP_MCP_START_TIMEOUT_SECONDS", raw)
    assert worker_mcp.start_timeout_s() == worker_mcp.DEFAULT_START_TIMEOUT_S


def test_a_hanging_start_is_bounded_by_the_deadline(engine, monkeypatch):
    monkeypatch.setenv("EAP_MCP_START_TIMEOUT_SECONDS", "1")

    class Hang(FakeRuntime):
        async def start(self, spec):
            await asyncio.sleep(60)

    _, rid = add_managed(engine, "t1", state="stopped")
    with pytest.raises(McpToolUnavailable) as exc:
        asyncio.run(asyncio.wait_for(ensure_managed_running(engine, "t1", conn_for(engine, Hang()), "t", runtime=Hang()), 10))
    assert exc.value.code == "runtime_start_failed" and state_of(engine, rid).state == "failed"


def test_a_hanging_status_is_bounded_by_the_deadline(engine, monkeypatch):
    monkeypatch.setenv("EAP_MCP_START_TIMEOUT_SECONDS", "1")

    class Hang(FakeRuntime):
        async def status(self, handle):
            await asyncio.sleep(60)

    _, rid = add_managed(engine, "t1", state="starting")
    with pytest.raises(McpToolUnavailable) as exc:
        asyncio.run(asyncio.wait_for(ensure_managed_running(engine, "t1", conn_for(engine, Hang()), "t", runtime=Hang()), 10))
    assert exc.value.code == "runtime_start_timeout"


def test_waiters_exit_when_the_starter_marks_the_row_failed(engine):
    class Lagging(FakeRuntime):
        async def status(self, handle):
            if not self.calls:
                with engine.begin() as c:
                    c.execute(text("UPDATE mcp_runtimes SET state = 'failed', last_error = 'start_failed: X'"))
            self.calls.append(("status", handle))
            return RuntimeStatus("starting", None)  # type: ignore[arg-type]

    _, rid = add_managed(engine, "t1", state="starting")
    fake = Lagging()
    with pytest.raises(McpToolUnavailable) as exc:
        asyncio.run(ensure_managed_running(engine, "t1", conn_for(engine, fake), "t", runtime=fake))
    assert exc.value.code == "runtime_failed"
    assert fake.kinds().count("status") <= 2


def test_a_non_managed_row_without_a_url_is_unavailable(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO mcp_servers (id, tenant_id, name, url, runtime) VALUES ('s1', 't1', 'memory', NULL, 'external')"))
    with pytest.raises(McpToolUnavailable) as exc:
        load_servers(engine, "t1", refs())
    assert exc.value.code == "server_url_missing"
