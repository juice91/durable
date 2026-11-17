"""`OutboundPolicy.for_managed_runtime`: a managed pod's endpoint is allowed by identity, nothing else."""

from __future__ import annotations

import logging

import anyio
import pytest

from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV, OutboundBlocked, OutboundPolicy

ENDPOINT = "http://mcp-abc-proxy.mcp-t1.svc:8080/mcp"


def fixed(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def managed() -> OutboundPolicy:
    return OutboundPolicy(resolver=fixed("10.43.7.9")).for_managed_runtime("rt-1", ENDPOINT)


def test_the_endpoint_itself_is_allowed_over_http_and_resolves_to_a_private_address() -> None:
    policy = managed()
    target = policy.check_url(ENDPOINT)
    assert (target.scheme, target.host, target.port) == ("http", "mcp-abc-proxy.mcp-t1.svc", 8080)
    assert anyio.run(policy.check, ENDPOINT).host == "mcp-abc-proxy.mcp-t1.svc"


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp-other-proxy.mcp-t2.svc:8080/mcp",  # another tenant's pod
        "http://mcp-abc-proxy.mcp-t1.svc:9090/mcp",  # same host, another port
        "http://mcp-abc-proxy.mcp-t1.svc/mcp",  # default port 80
        "https://mcp-abc-proxy.mcp-t1.svc:8080/mcp",  # another scheme
        "http://10.43.7.9:8080/mcp",  # the address instead of the identity
        "http://postgres.pydanticos.svc:5432/",
        "http://169.254.169.254/latest/meta-data",
        "https://example.com/mcp",
    ],
)
def test_anything_else_is_refused(url: str) -> None:
    with pytest.raises(OutboundBlocked) as exc:
        managed().check_url(url)
    assert exc.value.code == "managed_endpoint_mismatch"


def test_credentials_in_the_url_are_still_refused() -> None:
    with pytest.raises(OutboundBlocked) as exc:
        managed().check_url("http://user:pw@mcp-abc-proxy.mcp-t1.svc:8080/mcp")
    assert exc.value.code == "url_credentials"


def test_connect_time_resolution_allows_only_the_endpoint_host_and_port() -> None:
    policy = managed()
    assert anyio.run(policy.resolve_checked, "mcp-abc-proxy.mcp-t1.svc", 8080) == ["10.43.7.9"]
    for host, port in (("mcp-abc-proxy.mcp-t1.svc", 9090), ("other.svc", 8080)):
        with pytest.raises(OutboundBlocked) as exc:
            anyio.run(policy.resolve_checked, host, port)
        assert exc.value.code == "managed_endpoint_mismatch"


def test_the_dev_allowlist_never_applies_to_a_managed_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEV_ALLOW_HOSTS_ENV, "localhost,127.0.0.1,other.svc")
    policy = OutboundPolicy.from_env(resolver=fixed("127.0.0.1")).for_managed_runtime("rt-1", ENDPOINT)
    assert policy.dev_allow_hosts == frozenset()
    with pytest.raises(OutboundBlocked):
        policy.check_url("http://127.0.0.1:8080/mcp")
    with pytest.raises(OutboundBlocked):
        policy.check_url("http://other.svc:8080/mcp")


def test_the_plain_policy_still_blocks_a_private_cluster_address() -> None:
    plain = OutboundPolicy(resolver=fixed("10.43.7.9"))
    with pytest.raises(OutboundBlocked) as exc:
        anyio.run(plain.check, "https://mcp-abc-proxy.mcp-t1.svc/mcp")
    assert exc.value.code == "address_blocked"


@pytest.mark.parametrize("endpoint", ["", "ftp://x.svc/", "http:///mcp", "http://u:p@x.svc/", "http://x.svc:99999/"])
def test_a_malformed_endpoint_is_refused(endpoint: str) -> None:
    with pytest.raises(OutboundBlocked) as exc:
        OutboundPolicy().for_managed_runtime("rt-1", endpoint)
    assert exc.value.code == "managed_endpoint_invalid"


def test_the_grant_is_logged_by_identity(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="eap_secrets.outbound"):
        managed()
    assert any("rt-1" in r.getMessage() and "mcp-abc-proxy.mcp-t1.svc:8080" in r.getMessage() for r in caplog.records)
