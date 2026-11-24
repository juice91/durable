"""A transformer from the OpenFGA DSL (schema 1.1) to the JSON the API stores.

The OpenFGA CLI and the Go and JS libraries ship one; Python has none, and a deployment that only carries the API image
has no CLI. This one supports exactly what `durable.fga` uses: types, `define name: expr` where expr is `or`-joined
terms of `[type, type#relation]`, `relation` and `relation from tupleset`. Anything else raises: a model that needs more
is transformed by the CLI. CI compares this output with `fga model transform` for the shipped model, so the two cannot
drift (`python -m eap_iam_openfga.dsl <file>` prints it)."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_TYPE_RE = re.compile(rf"^type ({_NAME})$")
_DEFINE_RE = re.compile(rf"^define ({_NAME}): (.+)$")
_TTU_RE = re.compile(rf"^({_NAME}) from ({_NAME})$")
_REL_RE = re.compile(rf"^{_NAME}$")
_DIRECT_RE = re.compile(rf"^\[\s*(.+?)\s*\]$")
_DIRECT_TYPE_RE = re.compile(rf"^({_NAME})(?:#({_NAME}))?$")


class ModelSyntaxError(ValueError):
    pass


def _direct(body: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for part in body.split(","):
        m = _DIRECT_TYPE_RE.match(part.strip())
        if not m:
            raise ModelSyntaxError(f"unsupported direct type {part.strip()!r}")
        found.append({"type": m.group(1), **({"relation": m.group(2)} if m.group(2) else {})})
    return found


def _expression(expr: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    children: list[dict[str, Any]] = []
    direct: list[dict[str, str]] = []
    if " and " in f" {expr} " or " but not " in expr:
        raise ModelSyntaxError(f"only `or` is supported: {expr!r}")
    for term in re.split(r"\s+or\s+", expr.strip()):
        if m := _DIRECT_RE.match(term):
            direct += _direct(m.group(1))
            children.append({"this": {}})
        elif m := _TTU_RE.match(term):
            children.append(
                {"tupleToUserset": {"tupleset": {"relation": m.group(2)}, "computedUserset": {"relation": m.group(1)}}}
            )
        elif _REL_RE.match(term):
            children.append({"computedUserset": {"relation": term}})
        else:
            raise ModelSyntaxError(f"unsupported term {term!r}")
    return (children[0] if len(children) == 1 else {"union": {"child": children}}), direct


def parse_model(text: str) -> dict[str, Any]:
    """The model as the JSON `POST /stores/{id}/authorization-models` takes."""
    types: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_relations = False
    schema = None
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = "" if raw.lstrip().startswith("#") else raw.strip()
        if not stripped:
            continue
        try:
            if stripped == "model":
                continue
            if stripped.startswith("schema "):
                schema = stripped.split(" ", 1)[1].strip()
                if schema != "1.1":
                    raise ModelSyntaxError(f"schema {schema} is not supported (1.1 only)")
            elif m := _TYPE_RE.match(stripped):
                current = {"type": m.group(1)}
                types.append(current)
                in_relations = False
            elif stripped == "relations":
                if current is None:
                    raise ModelSyntaxError("`relations` outside a type")
                current["relations"] = {}
                current["metadata"] = {"relations": {}}
                in_relations = True
            elif m := _DEFINE_RE.match(stripped):
                if current is None or not in_relations:
                    raise ModelSyntaxError("`define` outside `relations`")
                if m.group(1) in current["relations"]:
                    raise ModelSyntaxError(f"relation {m.group(1)!r} defined twice")
                rewrite, direct = _expression(m.group(2))
                current["relations"][m.group(1)] = rewrite
                current["metadata"]["relations"][m.group(1)] = {"directly_related_user_types": direct} if direct else {}
            else:
                raise ModelSyntaxError(f"cannot read {stripped!r}")
        except ModelSyntaxError as exc:
            raise ModelSyntaxError(f"line {number}: {exc}") from None
    if schema is None:
        raise ModelSyntaxError("no `schema 1.1` line")
    return {"schema_version": schema, "type_definitions": types}


def relations_of(model: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Type name -> its relation names."""
    return {t["type"]: frozenset(t.get("relations", {})) for t in model["type_definitions"]}


def canonical(model: dict[str, Any]) -> str:
    """A comparison form: no `id`, types sorted, keys sorted. Two models with the same canonical form are the same
    model, whatever order the server returned them in."""
    types = sorted(({k: v for k, v in t.items()} for t in model["type_definitions"]), key=lambda t: t["type"])
    return json.dumps({"schema_version": model["schema_version"], "type_definitions": types}, sort_keys=True)


def _clean(value: Any) -> Any:
    """Drop the empty-string defaults the server fills in (`"object": ""`)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v != ""}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def semantic(model: dict[str, Any]) -> str:
    """What a model *means*, in a form that survives a round trip through the server: the rewrite of every relation and
    the directly related user types of every relation. OpenFGA stores a model with default fields filled in (empty
    `condition`, `module`, `source_info`), so two models are compared on this, not byte for byte."""
    types: dict[str, Any] = {}
    for t in model["type_definitions"]:
        direct = {
            rel: sorted(
                (d["type"], d.get("relation") or "")
                for d in (meta.get("directly_related_user_types") or [])
            )
            for rel, meta in ((t.get("metadata") or {}).get("relations") or {}).items()
        }
        types[t["type"]] = {"relations": _clean(t.get("relations") or {}), "direct": {r: d for r, d in direct.items() if d}}
    return json.dumps({"schema_version": model.get("schema_version"), "types": types}, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m eap_iam_openfga.dsl <model.fga>", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as handle:
        print(json.dumps(parse_model(handle.read()), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
