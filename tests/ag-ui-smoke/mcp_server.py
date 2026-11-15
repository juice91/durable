"""The smoke test's MCP server: FastMCP over Streamable HTTP on 127.0.0.1 (AGUI.14).

Tools:
- `lookup_order(order_id)`        -> {"order_id", "status": "shipped"}
- `slow_lookup(order_id, seconds)` -> the same after sleeping `seconds` (at most 30), so a turn stays
                                     running long enough for the previous-turn healing scenario.

`GET /calls` answers the executed calls as JSON `[{"tool", "args"}, ...]`, so the smoke can check
that a server tool ran exactly once. Run: `uv run python tests/ag-ui-smoke/mcp_server.py <port>`;
the worker must allow the host (`EAP_MCP_DEV_ALLOW_HOSTS=127.0.0.1`).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import uvicorn
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("agui-smoke")
_calls: list[dict[str, Any]] = []
_lock = threading.Lock()


def _record(tool: str, args: dict[str, Any]) -> None:
    with _lock:
        _calls.append({"tool": tool, "args": args})


@mcp.tool
def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order's status."""
    _record("lookup_order", {"order_id": order_id})
    return {"order_id": order_id, "status": "shipped"}


@mcp.tool
def slow_lookup(order_id: str, seconds: float = 4.0) -> dict[str, str]:
    """Look up an order's status slowly."""
    _record("slow_lookup", {"order_id": order_id, "seconds": seconds})
    time.sleep(max(0.0, min(float(seconds), 30.0)))
    return {"order_id": order_id, "status": "shipped"}


@mcp.custom_route("/calls", methods=["GET"])
async def calls(_: Request) -> JSONResponse:
    with _lock:
        return JSONResponse(list(_calls))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8209
    uvicorn.run(mcp.http_app(path="/mcp"), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
