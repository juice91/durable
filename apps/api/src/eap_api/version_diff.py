"""Version compare: field-level changes between two agent specs and a line diff of the instructions.

Pure functions over spec JSON, no I/O. Used by GET /agents/{id}/versions/compare.
"""

from __future__ import annotations

import difflib
from typing import Any

from pydantic import ValidationError

from eap_schemas import AgentSpec

# A change list bigger than this is cut (with `truncated: true`): specs are bounded, but a compare
# must never return an unbounded body.
MAX_CHANGES = 500

# difflib's SequenceMatcher is quadratic in lines: past these sizes the instructions diff is one
# coarse block replace (`instructions_diff_truncated: true`) so a compare stays fast on any input.
MAX_DIFF_LINES = 2_000
MAX_DIFF_LINE_PRODUCT = 4_000_000


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The spec as today's AgentSpec dumps it, so defaults added since a version was published do
    not show up as changes. A spec that no longer validates is compared as stored.

    The agent-capabilities fields (`output`, `approvals`, `limits`, `fallback_models`) diff like any
    other: absent while empty, so adding one is a single `added` entry; inside them every leaf has
    its own path (`output.json_schema.properties.x`, `fallback_models[1]`). AgentSpec normalises
    `limits.max_cost_usd`, so "0.50" and "0.5" are no change."""
    try:
        return AgentSpec.model_validate(spec).model_dump(mode="json")
    except ValidationError:
        return spec


def _child(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def spec_changes(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Leaf-level changes from `before` to `after`, as `{path, kind, before, after}`.

    `kind` is `added`, `removed` or `changed`. Objects are compared key by key (sorted), lists index
    by index; a value whose JSON type differs (e.g. object vs list) is one `changed` entry. Paths are
    dotted with `[i]` for list items, e.g. `model.name`, `tools[1]`, `mcp_tools[0].server`.
    """
    changes: list[dict[str, Any]] = []
    _walk(before, after, path, changes)
    return changes


def _walk(before: Any, after: Any, path: str, out: list[dict[str, Any]]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            p = _child(path, key)
            if key not in before:
                out.append({"path": p, "kind": "added", "before": None, "after": after[key]})
            elif key not in after:
                out.append({"path": p, "kind": "removed", "before": before[key], "after": None})
            else:
                _walk(before[key], after[key], p, out)
        return
    if isinstance(before, list) and isinstance(after, list):
        for i in range(max(len(before), len(after))):
            p = _child(path, i)
            if i >= len(before):
                out.append({"path": p, "kind": "added", "before": None, "after": after[i]})
            elif i >= len(after):
                out.append({"path": p, "kind": "removed", "before": before[i], "after": None})
            else:
                _walk(before[i], after[i], p, out)
        return
    # bool is an int in Python: compare type as well so `1` -> `true` is a change.
    if type(before) is not type(after) or before != after:
        out.append({"path": path, "kind": "changed", "before": before, "after": after})


def diff_too_large(a_lines: int, b_lines: int) -> bool:
    """Whether a line diff of texts this size would be too slow to compute line by line."""
    return (
        a_lines > MAX_DIFF_LINES or b_lines > MAX_DIFF_LINES or a_lines * b_lines > MAX_DIFF_LINE_PRODUCT
    )


def _coarse_diff(a: list[str], b: list[str]) -> list[dict[str, Any]]:
    """Every base line removed, then every target line added (identical texts: all equal)."""
    if a == b:
        return [{"op": "equal", "text": t, "base_line": i + 1, "target_line": i + 1} for i, t in enumerate(a)]
    removed = [{"op": "removed", "text": t, "base_line": i + 1, "target_line": None} for i, t in enumerate(a)]
    added = [{"op": "added", "text": t, "base_line": None, "target_line": j + 1} for j, t in enumerate(b)]
    return removed + added


def instructions_diff(before: str, after: str) -> list[dict[str, Any]]:
    """Line diff of two instruction texts: `{op, text, base_line, target_line}` per line, `op` one
    of `equal`, `added`, `removed`; line numbers are 1-based and null on the side the line is not in.
    Texts past the diff_too_large limits get a coarse whole-block replace instead."""
    a, b = before.splitlines(), after.splitlines()
    if diff_too_large(len(a), len(b)):
        return _coarse_diff(a, b)
    lines: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                lines.append({"op": "equal", "text": a[i1 + k], "base_line": i1 + k + 1, "target_line": j1 + k + 1})
            continue
        for i in range(i1, i2):  # replace = removed lines, then added lines
            lines.append({"op": "removed", "text": a[i], "base_line": i + 1, "target_line": None})
        for j in range(j1, j2):
            lines.append({"op": "added", "text": b[j], "base_line": None, "target_line": j + 1})
    return lines


def compare_specs(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """`changes` (field level, over the normalized specs), `truncated`, `instructions_diff`, and
    `instructions_diff_truncated` (true when the instructions were too long for a line diff and
    `instructions_diff` is a coarse whole-block replace)."""
    nb, nt = normalize_spec(base), normalize_spec(target)
    changes = spec_changes(nb, nt)
    instr_b = nb.get("instructions") if isinstance(nb.get("instructions"), str) else ""
    instr_t = nt.get("instructions") if isinstance(nt.get("instructions"), str) else ""
    return {
        "changes": changes[:MAX_CHANGES],
        "truncated": len(changes) > MAX_CHANGES,
        "instructions_diff": instructions_diff(instr_b, instr_t),
        "instructions_diff_truncated": diff_too_large(len(instr_b.splitlines()), len(instr_t.splitlines())),
    }


__all__ = [
    "MAX_CHANGES",
    "MAX_DIFF_LINES",
    "MAX_DIFF_LINE_PRODUCT",
    "compare_specs",
    "diff_too_large",
    "instructions_diff",
    "normalize_spec",
    "spec_changes",
]
