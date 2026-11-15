"""The real API in its own process, for the whole-system recovery and safety tests.

Run as `python tests/recovery/api_server.py` with the test's EAP_* environment. It binds a free port
on 127.0.0.1 itself (so there is no race picking one), serves `eap_api.app:create_app` with uvicorn,
logs at INFO to stdout like the worker (`python -m eap_worker`), and prints `EAP_API_READY port=<n>`
once /healthz answers. SIGTERM stops uvicorn gracefully, so telemetry is flushed at exit.

`EAP_TEST_DNS_STUB="name=addr[|addr...],..."` (tests only) makes the process resolve those exact
host names to those addresses (socket.getaddrinfo, which the outbound policy's resolver uses), so a
test can register a DNS name that resolves to a private address without editing /etc/hosts. Every
other name resolves normally.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request


def install_dns_stub(spec: str) -> None:
    table: dict[str, list[str]] = {}
    for entry in filter(None, (e.strip() for e in spec.split(","))):
        name, _, addrs = entry.partition("=")
        table[name.strip().lower()] = [a.strip() for a in addrs.split("|") if a.strip()]
    real = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - socket's signature
        name = host.decode() if isinstance(host, bytes) else host
        if isinstance(name, str) and name.lower().rstrip(".") in table:
            out = []
            for addr in table[name.lower().rstrip(".")]:
                out.extend(real(addr, port, family, type, proto, flags | socket.AI_NUMERICHOST))
            return out
        return real(host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo
    print(f"EAP_TEST_DNS_STUB active for {sorted(table)}", flush=True)


def main() -> None:
    if os.environ.get("EAP_TEST_DNS_STUB"):
        install_dns_stub(os.environ["EAP_TEST_DNS_STUB"])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config("eap_api.app:create_app", factory=True, log_level="info", log_config=None)
    server = uvicorn.Server(config)

    def announce() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                    if response.status == 200:
                        # One write, newline included: print() writes the text and its newline
                        # separately, and the API's own access log line for this /healthz request
                        # (P1.C2 `eap.access`, another thread) could land in between, gluing its
                        # timestamp onto the port (`port=584492026`).
                        sys.stdout.write(f"EAP_API_READY port={port}\n")
                        sys.stdout.flush()
                        return
            except OSError:
                time.sleep(0.1)

    threading.Thread(target=announce, daemon=True).start()
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
