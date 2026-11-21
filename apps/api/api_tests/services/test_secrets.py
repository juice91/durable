"""SecretService: name and body refusals, the in-use refusal, and which servers a changed secret restarts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eap_api.mcp_servers import managed
from eap_api.services import secrets as secrets_module
from eap_api.services.errors import InvalidInput, NotFound
from eap_api.services.secrets import SecretInUse, SecretService
from eap_run_store.definition_errors import DefinitionInvalid


class FakeSecretStore:
    def __init__(self) -> None:
        self.put_calls: list[tuple] = []
        self.deleted = True

    def put(self, *args):
        self.put_calls.append(args)

    def delete(self, tenant_id, name):
        return self.deleted


class FakeRuntime:
    def __init__(self) -> None:
        self.restarted: list[str] = []

    def restart_runtime(self, server, row):
        self.restarted.append(server.id)
        return True


class FakeDb:
    """`scalar` answers the server row, then the runtime row, of each `used` entry in turn."""

    def __init__(self, rows) -> None:
        self.rows = list(rows)

    def scalar(self, _query):
        return self.rows.pop(0)


def service(store=None, runtime=None, db=None):
    store = store or FakeSecretStore()
    return SecretService(None, db, runtime or FakeRuntime(), keyed_store=lambda engine: store), store


def test_a_bad_name_is_refused_with_its_code():
    svc, store = service()
    with pytest.raises(InvalidInput) as raised:
        svc.put("t", "u", "Bad Name", {"value": "x"})
    assert raised.value.code == "secret_name_invalid" and not store.put_calls


@pytest.mark.parametrize("payload", [["x"], {}, {"value": ""}, {"value": "x", "extra": 1}])
def test_a_bad_body_is_refused_without_echoing_it(payload):
    svc, store = service()
    with pytest.raises(DefinitionInvalid) as raised:
        svc.put("t", "u", "token", payload)
    assert not store.put_calls
    assert "extra" not in raised.value.detail or raised.value.detail.startswith("extra")  # a location, never a value


def test_put_stores_the_value_then_restarts_only_enabled_servers_with_a_restartable_pod(monkeypatch):
    used = [{"server_id": "a", "name": "A"}, {"server_id": "b", "name": "B"}, {"server_id": "c", "name": "C"}]
    monkeypatch.setattr(managed, "servers_using", lambda db, tenant: {"token": used})
    server = lambda sid, enabled: SimpleNamespace(id=sid, enabled=enabled)  # noqa: E731
    row = lambda state: SimpleNamespace(state=state)  # noqa: E731
    db = FakeDb([
        server("a", True), row("running"),    # restarted
        server("b", False), row("running"),   # disabled
        server("c", True), row("stopped"),    # no pod to restart
    ])
    runtime = FakeRuntime()
    svc, store = service(runtime=runtime, db=db)
    out = svc.put("t", "alice", "token", {"value": "s3cret"})
    assert out == {"restarted": ["a"]} and runtime.restarted == ["a"]
    assert store.put_calls == [("t", "token", "s3cret", "alice")]


def test_delete_refuses_a_secret_a_managed_server_uses(monkeypatch):
    monkeypatch.setattr(managed, "servers_using", lambda db, tenant: {"token": [{"server_id": "a", "name": "A"}]})
    svc, _ = service()
    with pytest.raises(SecretInUse) as raised:
        svc.delete("t", "token")
    assert raised.value.code == "secret_in_use" and raised.value.servers == [{"server_id": "a", "name": "A"}]


def test_delete_of_a_missing_secret_is_not_found(monkeypatch):
    monkeypatch.setattr(managed, "servers_using", lambda db, tenant: {})
    monkeypatch.setattr(secrets_module, "SecretStore", lambda engine, ring: FakeSecretStore())
    svc, store = service()
    monkeypatch.setattr(secrets_module, "SecretStore", lambda engine, ring: SimpleNamespace(delete=lambda t, n: False))
    with pytest.raises(NotFound) as raised:
        svc.delete("t", "gone")
    assert raised.value.code == "secret_not_found"
