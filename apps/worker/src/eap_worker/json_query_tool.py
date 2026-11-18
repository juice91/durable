"""`json.query`: a JMESPath expression over JSON data. Pure: no I/O.

A workflow step uses it to pick a field, filter a list or reshape an earlier step's output (`items[?id=='c3'] | [0]`,
`{links: feed[].post.record.embed.external.uri}`).

- Arguments: `eap_schemas.tools.JsonQueryInput` (`data`: any JSON, at most 1 MiB; `expression`: at most 500
  characters).
- Result: `{result}`; a path that matches nothing is `{result: null}`.
- Errors, as `{error: {code, message}}` with fixed texts (the data and the expression are never echoed):
  `expression_invalid` (does not parse, or a function got a value of the wrong type) and `result_too_large` (the
  result is over 256 KiB serialised).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import jmespath
from jmespath import exceptions as jmes_exceptions
from pydantic import Field
from pydantic_ai import RunContext

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.tools import JSON_QUERY_RESULT_MAX_BYTES, JSON_QUERY_TOOL_ID, TOOL_DECLARATIONS


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def json_query(
    ctx: RunContext[RunDeps],
    data: Any,
    expression: Annotated[str, Field(max_length=500)],
) -> dict[str, Any]:
    """Evaluate a JMESPath expression (see the module docstring)."""
    try:
        result = jmespath.search(expression, data)
    except (jmes_exceptions.JMESPathError, ValueError, TypeError, RecursionError):
        return _error("expression_invalid", "the expression is not valid JMESPath or does not fit the data")
    if len(json.dumps(result, ensure_ascii=False, default=str).encode()) > JSON_QUERY_RESULT_MAX_BYTES:
        return _error("result_too_large", "the result is larger than 256 KiB; select less")
    return {"result": result}


def register_json_query_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[JSON_QUERY_TOOL_ID], json_query)


__all__ = ["json_query", "register_json_query_tool"]
