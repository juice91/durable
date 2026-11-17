"""Skip decision for tests that need a real Loki."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import requests

SKIP_REASON = "Loki is not reachable at 127.0.0.1:3100 (set EAP_REQUIRE_LOKI=1 to fail instead)"


def loki_reachable(url: str, *, timeout: float = 2.0) -> bool:
    try:
        return requests.get(f"{url}/ready", timeout=timeout).status_code == 200
    except requests.RequestException:
        return False


def should_skip_live(
    url: str, env: Mapping[str, str], probe: Callable[[str], bool] = loki_reachable
) -> bool:
    """True when the live Loki tests should skip: Loki is unreachable and EAP_REQUIRE_LOKI is not 1."""
    if env.get("EAP_REQUIRE_LOKI") == "1":
        return False
    return not probe(url)
