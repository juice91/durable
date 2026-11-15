"""Test suite import and export in the `pydantic_evals` dataset file format (AC.12).

The file shape is `pydantic_evals.Dataset.to_file`'s, in YAML or JSON:

```yaml
name: smoke
evaluators:
  - EqualsExpected
  - Contains: {value: hello, case_sensitive: false, evaluation_name: greets}
cases:
  - name: greeting
    inputs: Say hello
    expected_output: hello
    metadata: {area: chat}
    evaluators:
      - LLMJudge: {rubric: "Polite. MUST_CONTAIN:hello", model: "openai:gpt-5.2",
                   assertion: {evaluation_name: polite}}
```

An evaluator is an `EvaluatorSpec`: `ClassName` (no arguments), `{ClassName: <first positional
argument>}` or `{ClassName: {keyword arguments}}`.

**Export** writes only arguments the target class accepts (checked against pydantic_evals
2.67.0). A stock class is used whenever our evaluator fits its signature; otherwise one of ours,
so nothing is lost on a round trip:

| kind              | written as                                                                       |
|-------------------|----------------------------------------------------------------------------------|
| `equals_expected` | `EqualsExpected(evaluation_name?)` (stock)                                       |
| `contains`        | `Contains(value, case_sensitive?, evaluation_name?)` (stock) when it has a value; |
|                   | `ContainsExpected(case_sensitive?, evaluation_name?)` (ours) without one        |
| `max_duration`    | `MaxDuration(seconds)` (stock) unnamed; `NamedMaxDuration(seconds,              |
|                   | evaluation_name)` (ours) named (stock MaxDuration takes no name)                 |
| `llm_judge`       | `LLMJudge(rubric, model "provider:name", include_input?, include_expected_output?, |
|                   | assertion: {evaluation_name}?)` (stock) without `pass_threshold`;               |
|                   | `ThresholdLLMJudge(the same, pass_threshold)` (ours) with one                    |
| `regex`           | `Regex(pattern, evaluation_name?)` (ours, RE2)                                   |
| `schema_valid`    | `SchemaValid(json_schema?, evaluation_name?)` (ours)                             |
| `field_equals`    | `FieldEquals(path, value, evaluation_name?)` (ours)                              |
| `max_cost`        | `MaxCost(usd, evaluation_name?)` (ours; a number, or decimal text when a float   |
|                   | would round it)                                                                  |

To run an exported file locally, pass our classes as `custom_evaluator_types`, e.g.:

```python
from dataclasses import dataclass
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

@dataclass
class Regex(Evaluator):
    pattern: str
    evaluation_name: str | None = None
    def evaluate(self, ctx: EvaluatorContext) -> bool:
        import re
        return re.search(self.pattern, str(ctx.output)) is not None

# ContainsExpected(case_sensitive=True, evaluation_name=None), NamedMaxDuration(seconds,
# evaluation_name), ThresholdLLMJudge(rubric, model, include_input=False,
# include_expected_output=False, assertion=None, pass_threshold=None), SchemaValid(json_schema=None,
# evaluation_name=None), FieldEquals(path, value, evaluation_name=None) and MaxCost(usd,
# evaluation_name=None) follow the same pattern.

dataset = Dataset[str, object, dict].from_file("suite.yaml", custom_evaluator_types=[Regex, ...])
```

**Import** reads all of the above, plus stock files' common extras: `LLMJudge`'s `assertion`
(`evaluation_name` becomes our `name`; `include_reason` is ignored) and `score` (ignored: our
judge always records its score); `Contains`' `as_strings: false`. Anything else unknown is an
error (`EvalFormatError`, a 422), as are unknown classes, non-text `inputs`, a case without a
`name`, `LLMJudge` without `model` (the judge model must be named and allowlisted),
`model_settings`, `as_strings: true` and `assertion: false`.

**Parsing cost.** YAML is parsed once, with libyaml's `CSafeLoader` when available (the C form of
`yaml.safe_load`: no Python tags), else `SafeLoader`. Before that, `prescan_yaml` rejects a file
in one linear pass over the text when its flow or indent nesting, its alias-like tokens, its line
count or its value count (lines plus flow-collection items) go over a cap. The caps bound the
parse: a worst-case 1 MiB file within them parses in about 1.2 s of CPU, a normal 500-case
suite in about 0.1 s. After loading, a node reached twice (an alias) is refused, so no alias
expansion survives. The API never imports `pydantic_evals`. No I/O.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Literal

import logging

import yaml
from pydantic import TypeAdapter, ValidationError

from eap_schemas.evals import EvaluatorSpec

ExportFormat = Literal["yaml", "json"]
MAX_IMPORT_BYTES = 1024 * 1024
# Prescan caps (a 500-case suite uses roughly 5 to 20 lines per case and nests 6 to 10 levels).
MAX_YAML_DEPTH = 64
MAX_YAML_LINES = 50_000
MAX_YAML_ALIAS_TOKENS = 100
# Lines plus flow-collection items: the parse cost bound (libyaml builds ~200k nodes a second here).
MAX_YAML_NODES = 100_000

_YAML_LOADER: type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
if _YAML_LOADER is yaml.SafeLoader:
    # The prescan caps were measured against libyaml; the pure-Python loader is several times slower.
    logging.getLogger(__name__).warning(
        "PyYAML has no libyaml (CSafeLoader); eval suite YAML imports use the slower pure-Python loader",
        extra={"code": "yaml_libyaml_missing"},
    )

KIND_TO_CLASS: dict[str, str] = {
    "equals_expected": "EqualsExpected",
    "contains": "Contains",
    "max_duration": "MaxDuration",
    "regex": "Regex",
    "schema_valid": "SchemaValid",
    "field_equals": "FieldEquals",
    "max_cost": "MaxCost",
    "llm_judge": "LLMJudge",
}
# Every class import accepts (export writes the variants when a stock class cannot carry the data).
CLASS_TO_KIND: dict[str, str] = {
    **{cls: kind for kind, cls in KIND_TO_CLASS.items()},
    "ContainsExpected": "contains",
    "NamedMaxDuration": "max_duration",
    "ThresholdLLMJudge": "llm_judge",
}
# The keyword a short-form `{ClassName: value}` fills (its first positional argument).
_FIRST_ARGUMENT: dict[str, str] = {
    "Contains": "value",
    "MaxDuration": "seconds",
    "NamedMaxDuration": "seconds",
    "Regex": "pattern",
    "SchemaValid": "json_schema",
    "FieldEquals": "path",
    "MaxCost": "usd",
    "LLMJudge": "rubric",
    "ThresholdLLMJudge": "rubric",
}
_DATASET_KEYS = frozenset({"name", "cases", "evaluators", "report_evaluators", "$schema"})
_CASE_KEYS = frozenset({"name", "inputs", "metadata", "expected_output", "evaluators"})

_EVALUATOR = TypeAdapter(EvaluatorSpec)


class EvalFormatError(ValueError):
    """The file is not a valid suite in the `pydantic_evals` format. Pickle-safe."""

    def __init__(self, reason: str, path: str = "") -> None:
        self.reason = reason
        self.path = path
        super().__init__(reason, path)

    def __str__(self) -> str:
        where = f" at {self.path}" if self.path else ""
        return f"the suite file is invalid{where}: {self.reason}"


# ---------------------------------------------------------------------------- export


def _usd(value: Decimal) -> float | str:
    """A cost as a number when a float carries it exactly, else as decimal text."""
    as_float = float(value)
    return as_float if Decimal(repr(as_float)) == value else format(value, "f")


def evaluator_to_spec(evaluator: Any) -> str | dict[str, Any]:
    """One of our evaluator models as a `pydantic_evals` EvaluatorSpec (defaults left out), with
    only the arguments its class accepts (see the module docstring)."""
    kind = evaluator.kind
    cls = KIND_TO_CLASS[kind]
    args: dict[str, Any] = {}
    named = evaluator.name is not None
    if kind == "contains":
        if evaluator.value is None:
            cls = "ContainsExpected"
        else:
            args["value"] = evaluator.value
        if not evaluator.case_sensitive:
            args["case_sensitive"] = False
    elif kind == "max_duration":
        args["seconds"] = evaluator.seconds
        if named:
            cls = "NamedMaxDuration"
    elif kind == "regex":
        args["pattern"] = evaluator.pattern
    elif kind == "schema_valid":
        if evaluator.json_schema is not None:
            args["json_schema"] = evaluator.json_schema
    elif kind == "field_equals":
        args["path"] = evaluator.path
        args["value"] = evaluator.value
    elif kind == "max_cost":
        args["usd"] = _usd(evaluator.usd)
    elif kind == "llm_judge":
        args["rubric"] = evaluator.rubric
        args["model"] = f"{evaluator.model.provider}:{evaluator.model.name}"
        if evaluator.include_input:
            args["include_input"] = True
        if evaluator.include_expected:
            args["include_expected_output"] = True
        if named:
            args["assertion"] = {"evaluation_name": evaluator.name}
        if evaluator.pass_threshold is not None:
            cls = "ThresholdLLMJudge"
            args["pass_threshold"] = evaluator.pass_threshold
    if named and kind not in ("llm_judge",) and cls != "MaxDuration":
        args["evaluation_name"] = evaluator.name
    return {cls: args} if args else cls


def dataset_dict(name: str, evaluators: Any, cases: Any) -> dict[str, Any]:
    """The `Dataset` file content of a suite: `cases` are EvalCase-like (name, input,
    expected_output, metadata, evaluators), in execution order. Empty optional keys are left out."""
    out_cases = []
    for case in cases:
        item: dict[str, Any] = {"name": case.name, "inputs": case.input}
        if case.expected_output is not None:
            item["expected_output"] = case.expected_output
        if case.metadata:
            item["metadata"] = dict(case.metadata)
        if case.evaluators:
            item["evaluators"] = [evaluator_to_spec(e) for e in case.evaluators]
        out_cases.append(item)
    return {"name": name, "cases": out_cases, "evaluators": [evaluator_to_spec(e) for e in evaluators]}


def dump_dataset(data: dict[str, Any], fmt: ExportFormat) -> str:
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)


# ---------------------------------------------------------------------------- import


def _model_ref(value: Any, path: str) -> Any:
    """`provider:name` text (the pydantic_evals form) as a ModelRef mapping; a mapping passes."""
    if isinstance(value, str):
        provider, sep, name = value.partition(":")
        if not sep or not provider or not name:
            raise EvalFormatError("model must be 'provider:name'", path)
        return {"provider": provider, "name": name}
    return value


def _judge_args(cls: str, args: dict[str, Any], path: str) -> None:
    """Stock LLMJudge arguments mapped onto ours, in place."""
    if "model_settings" in args:
        raise EvalFormatError(f"{cls}: model_settings is not supported", path)
    if "model" not in args:
        raise EvalFormatError(f"{cls}: model is required (name an allowlisted judge model)", path)
    args["model"] = _model_ref(args["model"], f"{path}.model")
    if "include_expected_output" in args:
        args["include_expected"] = args.pop("include_expected_output")
    args.pop("score", None)  # our judge always records its 0..1 score
    if "assertion" in args:
        assertion = args.pop("assertion")
        if assertion is False:
            raise EvalFormatError(f"{cls}: assertion: false is not supported (the judge's pass is the assertion)", path)
        if assertion is not None:
            if not isinstance(assertion, dict) or not set(assertion) <= {"evaluation_name", "include_reason"}:
                raise EvalFormatError(f"{cls}: assertion must be {{evaluation_name?, include_reason?}}", path)
            if "evaluation_name" in assertion:
                if "name" in args:
                    raise EvalFormatError(f"{cls}: give the name once (assertion.evaluation_name)", path)
                args["name"] = assertion["evaluation_name"]
    if cls == "LLMJudge" and "pass_threshold" in args:
        raise EvalFormatError("LLMJudge takes no pass_threshold; use ThresholdLLMJudge", path)


def evaluator_from_spec(spec: Any, path: str) -> dict[str, Any]:
    """A `pydantic_evals` EvaluatorSpec as one of our evaluator dicts, validated."""
    if isinstance(spec, str):
        cls, raw = spec, None
    elif isinstance(spec, dict) and len(spec) == 1:
        ((cls, raw),) = spec.items()
    else:
        raise EvalFormatError("an evaluator is 'ClassName' or {ClassName: arguments}", path)
    if not isinstance(cls, str) or cls not in CLASS_TO_KIND:
        known = ", ".join(sorted(CLASS_TO_KIND))
        raise EvalFormatError(f"unknown evaluator {str(cls)[:100]!r}; known: {known}", path)
    if raw is None:
        args: dict[str, Any] = {}
    elif isinstance(raw, dict):
        args = dict(raw)
    elif cls in _FIRST_ARGUMENT:
        args = {_FIRST_ARGUMENT[cls]: raw}
    else:
        raise EvalFormatError(f"{cls} takes no positional argument", path)
    if not all(isinstance(k, str) for k in args):
        raise EvalFormatError("argument names must be text", path)
    if "kind" in args or "name" in args:
        raise EvalFormatError(f"unknown argument {'kind' if 'kind' in args else 'name'!r}", path)
    if "evaluation_name" in args:
        args["name"] = args.pop("evaluation_name")
    if cls in ("LLMJudge", "ThresholdLLMJudge"):
        _judge_args(cls, args, path)
    elif cls == "Contains":
        if args.pop("as_strings", False) is not False:
            raise EvalFormatError("Contains: as_strings: true is not supported", path)
    elif cls == "ContainsExpected" and "value" in args:
        raise EvalFormatError("ContainsExpected takes no value (it uses the expected output)", path)
    elif cls == "MaxDuration" and "name" in args:
        raise EvalFormatError("MaxDuration takes no evaluation_name; use NamedMaxDuration", path)
    try:
        evaluator = _EVALUATOR.validate_python({"kind": CLASS_TO_KIND[cls], **args})
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ())[1:])
        raise EvalFormatError(f"{cls}: {loc + ': ' if loc else ''}{err['msg']}", path) from None
    out = evaluator.model_dump(mode="json", exclude_none=True)
    if evaluator.kind == "field_equals":
        out["value"] = evaluator.value  # required, and null is a value to compare with
    return out


def _evaluator_list(value: Any, path: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EvalFormatError("evaluators must be a list", path)
    return [evaluator_from_spec(spec, f"{path}[{i}]") for i, spec in enumerate(value)]


_FLOW_CHARS = re.compile(r"[\[\]{},]")
# An alias (`*name`) or anchor (`&name`) where a node can start (line start, after `key: `, `- `,
# `[`, `{` or `,`) and ending as a token does, so `*emphasis*` in prose is not counted. It can
# still over-count inside quoted text; the cap is generous and real aliases are refused after
# loading anyway.
_ALIAS_TOKEN = re.compile(r"(?:^[ \t]*|[:\-][ \t]+|[\[{,][ \t]*)[*&][^\s,\[\]{}*&]+(?=[\s,\]}]|$)", re.MULTILINE)
_SEQ_DASHES = re.compile(r"(?:-[ \t]+)+")


def prescan_yaml(text: str) -> None:
    """Reject, in one linear pass over the text, a YAML file whose parse could be expensive: more
    than MAX_YAML_LINES lines, more than MAX_YAML_NODES values (lines plus flow-collection items),
    more than MAX_YAML_ALIAS_TOKENS alias or anchor tokens, or flow
    (`[`/`{`) or block (indent and `- - -`) nesting deeper than MAX_YAML_DEPTH. Quoted text counts
    too (a conservative over-estimate)."""
    if text.count("\n") + 1 > MAX_YAML_LINES:
        raise EvalFormatError(f"the file has more than {MAX_YAML_LINES} lines")
    aliases = 0
    for _ in _ALIAS_TOKEN.finditer(text):
        aliases += 1
        if aliases > MAX_YAML_ALIAS_TOKENS:
            raise EvalFormatError(f"the file has more than {MAX_YAML_ALIAS_TOKENS} anchors or aliases")
    lines = text.count("\n") + 1
    depth = 0
    flow_nodes = 0
    for match in _FLOW_CHARS.finditer(text):
        char = match.group()
        if char == ",":
            if depth > 0:  # a comma separates flow items; in block context it is plain text
                flow_nodes += 1
        elif char in "[{":
            depth += 1
            flow_nodes += 1
            if depth > MAX_YAML_DEPTH:
                raise EvalFormatError(f"the file is nested more than {MAX_YAML_DEPTH} levels deep")
        elif depth > 0:
            depth -= 1
    if lines + flow_nodes > MAX_YAML_NODES:
        raise EvalFormatError(
            f"the file has more than {MAX_YAML_NODES} values; split it, or write long lists one item per line"
        )
    indents: list[int] = []
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        while indents and indents[-1] > indent:
            indents.pop()
        if not indents or indents[-1] < indent:
            indents.append(indent)
        dashes = _SEQ_DASHES.match(stripped)
        extra = dashes.group().count("-") - 1 if dashes else 0
        if len(indents) + extra > MAX_YAML_DEPTH:
            raise EvalFormatError(f"the file is nested more than {MAX_YAML_DEPTH} levels deep")


def _reject_shared_nodes(data: Any) -> None:
    """A list or mapping reached twice is an alias: refused (no expansion survives the load)."""
    seen: set[int] = set()
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, (list, dict)):
            if id(node) in seen:
                raise EvalFormatError("YAML aliases (*name) are not supported")
            seen.add(id(node))
            stack.extend(node.values() if isinstance(node, dict) else node)


def parse_file(text: str, fmt: ExportFormat) -> Any:
    """The file's data: JSON, or YAML parsed once with the C safe loader (see the module docstring)."""
    try:
        if fmt == "json":
            return json.loads(text)
        prescan_yaml(text)
        data = yaml.load(text, Loader=_YAML_LOADER)  # noqa: S506 - a safe loader (CSafeLoader or SafeLoader)
        _reject_shared_nodes(data)
        return data
    except EvalFormatError:
        raise
    except json.JSONDecodeError as exc:
        raise EvalFormatError(f"not valid JSON (line {exc.lineno})") from None
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = f" (line {mark.line + 1})" if mark is not None else ""
        problem = "unsupported YAML tag" if isinstance(exc, yaml.constructor.ConstructorError) else "not valid YAML"
        raise EvalFormatError(f"{problem}{line}") from None
    except yaml.YAMLError:
        raise EvalFormatError("not valid YAML") from None
    except RecursionError:
        raise EvalFormatError("the file is nested too deeply") from None


def read_dataset(data: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """(cases, suite evaluators) of parsed file data. `cases` are EvalCase dicts (validated by the
    store); the evaluators are None when the file has no `evaluators` key (the suite's are kept)."""
    if not isinstance(data, dict):
        raise EvalFormatError("the file must be a mapping with a 'cases' list")
    unknown = sorted(str(k) for k in data if k not in _DATASET_KEYS)
    if unknown:
        raise EvalFormatError(f"unknown key {unknown[0][:100]!r}")
    if data.get("report_evaluators"):
        raise EvalFormatError("report evaluators are not supported", "report_evaluators")
    raw_cases = data.get("cases")
    if raw_cases is None:
        raw_cases = []
    if not isinstance(raw_cases, list):
        raise EvalFormatError("cases must be a list", "cases")
    evaluators = _evaluator_list(data["evaluators"], "evaluators") if "evaluators" in data else None
    cases: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_cases):
        path = f"cases[{i}]"
        if not isinstance(raw, dict):
            raise EvalFormatError("a case must be a mapping", path)
        unknown = sorted(str(k) for k in raw if k not in _CASE_KEYS)
        if unknown:
            raise EvalFormatError(f"unknown key {unknown[0][:100]!r}", path)
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise EvalFormatError("every case needs a name (cases are imported by name)", f"{path}.name")
        inputs = raw.get("inputs")
        if not isinstance(inputs, str):
            raise EvalFormatError("inputs must be text", f"{path}.inputs")
        metadata = raw.get("metadata")
        cases.append(
            {
                "name": name,
                "input": inputs,
                "expected_output": raw.get("expected_output"),
                "metadata": {} if metadata is None else metadata,
                "evaluators": _evaluator_list(raw.get("evaluators"), f"{path}.evaluators"),
            }
        )
    return cases, evaluators


__all__ = [
    "CLASS_TO_KIND",
    "EvalFormatError",
    "ExportFormat",
    "KIND_TO_CLASS",
    "MAX_IMPORT_BYTES",
    "MAX_YAML_ALIAS_TOKENS",
    "MAX_YAML_DEPTH",
    "MAX_YAML_LINES",
    "MAX_YAML_NODES",
    "dataset_dict",
    "dump_dataset",
    "evaluator_from_spec",
    "evaluator_to_spec",
    "parse_file",
    "prescan_yaml",
    "read_dataset",
]
