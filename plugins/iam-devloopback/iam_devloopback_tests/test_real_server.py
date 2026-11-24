"""The loopback rule against a real ASGI server: the same bearer is 200 from 127.0.0.1 and 401 from another address,
with and without `X-Forwarded-For: 127.0.0.1` (design 1.4, 10). The server runs with proxy headers off, as deployed."""

import socket
import threading
import time

import json
import urllib.error
import urllib.request

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from eap_iam import AuthError, IdentityChain, RequestView
from eap_iam_devloopback import DevLoopbackProvider

BEARER = {"Authorization": "Bearer dev:studio:operator"}


def build_app():
    app = FastAPI()
    chain = IdentityChain([DevLoopbackProvider({})])

    @app.get("/whoami")
    async def whoami(request: Request):
        view = RequestView(
            headers={k.lower(): v for k, v in request.headers.items()},
            peer_host=request.client.host if request.client else None,
            accepts=frozenset({"user"}),
        )
        try:
            principal = await chain.authenticate(view)
        except AuthError as exc:
            raise HTTPException(exc.status, exc.detail) from None
        return {"subject": principal.subject}

    return app


def get(url: str, headers: dict[str, str]):
    """(status, json body) of a GET; urllib sends the headers as given and never a proxy header of its own."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outbound interface
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


def _serve(host: str):
    """Run the app on one specific address (never a wildcard bind) and yield its port; stop it afterwards."""
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(build_app(), host=host, port=port, log_level="error", proxy_headers=False)
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert instance.started
    yield port
    instance.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def server(monkeypatch_module):
    monkeypatch_module.setenv("EAP_AUTH_MODE", "dev")
    yield from _serve("127.0.0.1")


@pytest.fixture(scope="module")
def lan_server(monkeypatch_module):
    """The same app bound to this host's own non-loopback address, so a request to it has a non-loopback peer."""
    address = lan_address()
    if address is None:
        pytest.skip("this host has no non-loopback IPv4 address")
    monkeypatch_module.setenv("EAP_AUTH_MODE", "dev")
    for port in _serve(address):
        yield address, port


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def test_loopback_peer_is_accepted(server):
    assert get(f"http://127.0.0.1:{server}/whoami", BEARER) == (200, {"subject": "operator"})


def test_a_forwarding_header_makes_even_a_loopback_peer_unauthorised(server):
    assert get(f"http://127.0.0.1:{server}/whoami", {**BEARER, "X-Forwarded-For": "127.0.0.1"})[0] == 401


@pytest.mark.parametrize("forwarded", [None, "127.0.0.1"])
def test_a_non_loopback_peer_is_refused_with_and_without_x_forwarded_for(lan_server, forwarded):
    address, port = lan_server
    headers = dict(BEARER)
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    assert get(f"http://{address}:{port}/whoami", headers)[0] == 401
