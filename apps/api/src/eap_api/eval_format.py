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
| `regex`           | `Regex(pattern, negate?, field?, evaluation_name?)` (ours, RE2)                  |
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
    negate: bool = False
    field: str | None = None
    evaluation_name: str | None = None
    def evaluate(self, ctx: EvaluatorContext) -> bool:
        import re
        text = str(ctx.output) if self.field is None else str(ctx.output[self.field])  # simplified
        return (re.search(self.pattern, text) is not None) != self.negate

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
suite in about 0.1 s. Composing refuses any anchor or alias (so no merge key or expansion
survives), and before constructing, every node must carry a JSON tag (no timestamps, `!!binary`,
`!!set` or local tags), and no mapping may repeat a key (refused with its path; JSON too). The API never imports `pydantic_evals`. No I/O.
"""

from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal
from typing import Any, Literal

import logging

import yaml
from pydantic import TypeAdapter, ValidationError

from eap_schemas.evals import EvaluatorSpec

# "csv" is import only (`parse_csv_cases`); export writes yaml or json.
ExportFormat = Literal["yaml", "json", "csv"]
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
        if evaluator.negate:
            args["negate"] = True
        if evaluator.field is not None:
            args["field"] = evaluator.field
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
    if fmt == "csv":
        raise ValueError("a suite is exported as yaml or json")
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


class _JsonYamlLoader(yaml.composer.Composer, _YAML_LOADER):  # type: ignore[misc, valid-type]
    """The safe loader (libyaml's parser when available) with PyYAML's Python composer first in the
    MRO (libyaml's own composer hides anchors): any anchor or alias is refused while composing, so
    nothing is ever shared or expanded."""

    def __init__(self, stream: str) -> None:
        _YAML_LOADER.__init__(self, stream)
        yaml.composer.Composer.__init__(self)

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent) or self.peek_event().anchor is not None:
            event = self.peek_event()
            line = f" (line {event.start_mark.line + 1})" if event.start_mark is not None else ""
            raise EvalFormatError(f"YAML anchors and aliases (&name, *name) are not supported{line}")
        return yaml.composer.Composer.compose_node(self, parent, index)


_JSON_TAGS = frozenset(
    f"tag:yaml.org,2002:{t}" for t in ("str", "int", "float", "bool", "null", "seq", "map")
)
_MERGE_TAG = "tag:yaml.org,2002:merge"
_STR_TAG = "tag:yaml.org,2002:str"


def _check_nodes(root: Any) -> None:
    """Refuse, with the path, what JSON cannot hold: a tag outside the JSON types (timestamps,
    `!!binary`, `!!set`, local tags, ...), a merge key (`<<`), a key that is not a string (a
    collection, or a scalar that resolves to a number, bool or null: `1:`, `true:`, `null:`; quote
    it), a duplicate key."""
    stack: list[tuple[Any, str]] = [(root, "")]
    while stack:
        node, path = stack.pop()
        if node.tag not in _JSON_TAGS:
            name = node.tag.removeprefix("tag:yaml.org,2002:")[:40]
            raise EvalFormatError(f"unsupported YAML tag {name!r}: only JSON values are allowed", path)
        if isinstance(node, yaml.SequenceNode):
            stack.extend((item, f"{path}[{i}]") for i, item in enumerate(node.value))
        elif isinstance(node, yaml.MappingNode):
            seen: set[tuple[str, str]] = set()
            for key, value in node.value:
                if key.tag == _MERGE_TAG:
                    raise EvalFormatError("YAML merge keys (<<) are not supported", path)
                if not isinstance(key, yaml.ScalarNode) or key.tag != _STR_TAG:
                    raise EvalFormatError("a mapping key must be text (quote a number, bool or null key)", path)
                where = f"{path}.{key.value[:100]}" if path else key.value[:100]
                if (key.tag, key.value) in seen:
                    raise EvalFormatError(f"duplicate key {key.value[:100]!r}", where)
                seen.add((key.tag, key.value))
                stack.append((key, where))
                stack.append((value, where))


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise EvalFormatError(f"duplicate key {key[:100]!r}")
        out[key] = value
    return out


def parse_file(text: str, fmt: ExportFormat) -> Any:
    """The file's data: JSON, or YAML parsed once with the C safe loader (see the module docstring)."""
    try:
        if fmt == "json":
            return json.loads(text, object_pairs_hook=_json_object)
        prescan_yaml(text)
        loader = _JsonYamlLoader(text)
        try:
            node = loader.get_single_node()
            if node is None:
                return None
            _check_nodes(node)
            return loader.construct_document(node)
        finally:
            loader.dispose()
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


class CsvFormatError(EvalFormatError):
    """A CSV import problem at one row (`row` counts records from 1 = the header line; `message`
    never quotes the cell). Pickle-safe."""

    def __init__(self, row: int, message: str) -> None:
        self.row = row
        self.message = message
        super().__init__(message, f"row {row}")

    def __str__(self) -> str:
        return f"the CSV file is invalid at row {self.row}: {self.message}"


CSV_COLUMNS = ("name", "input", "expected_output", "metadata")


def _csv_json(value: str) -> Any:
    """`value` parsed as JSON (NaN and Infinity are not JSON), or ValueError."""

    def refuse(constant: str) -> Any:
        raise ValueError(constant)

    return json.loads(value, parse_constant=refuse)


def parse_csv_cases(text: str) -> list[dict[str, Any]]:
    """The cases of a CSV file, as EvalCase dicts (`name`, `input`, `expected_output`, `metadata`).

    The header names the columns: `name` and `input` are required, `expected_output` and `metadata`
    optional, in any order; nothing else is accepted. `expected_output` is JSON when it parses,
    else the text itself, and empty means none; `metadata` is the JSON text of an object, or empty.
    Quoted cells may hold commas, quotes and line breaks. All or nothing: the first problem is a
    `CsvFormatError` naming its row (the header is row 1), and no case is returned. Blank lines are
    skipped, names must be unique in the file."""
    from eap_schemas.evals import EvalCase

    text = text.removeprefix("\ufeff")  # a spreadsheet's UTF-8 byte order mark
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
    except csv.Error:
        raise CsvFormatError(1, "the header is not valid CSV") from None
    if header is None:
        raise CsvFormatError(1, "the file is empty; the first line must be the column names")
    columns = [h.strip().lower() for h in header]
    unknown = [c for c in columns if c not in CSV_COLUMNS]
    if unknown:
        raise CsvFormatError(1, f"unknown column {unknown[0][:50]!r}; the columns are {', '.join(CSV_COLUMNS)}")
    if len(set(columns)) != len(columns):
        raise CsvFormatError(1, "a column appears twice")
    for required in ("name", "input"):
        if required not in columns:
            raise CsvFormatError(1, f"the {required} column is missing")

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    row = 1
    while True:
        row += 1
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error:
            raise CsvFormatError(row, "not valid CSV (an unclosed quote, or a cell that is too large)") from None
        if not record:
            continue  # a blank line
        if len(record) != len(columns):
            raise CsvFormatError(row, f"expected {len(columns)} cells, found {len(record)}")
        cells = dict(zip(columns, record))
        name = cells["name"].strip()
        if not name:
            raise CsvFormatError(row, "name is required")
        if name in seen:
            raise CsvFormatError(row, "name is used by an earlier row (cases are imported by name)")
        seen.add(name)
        expected: Any = None
        raw_expected = cells.get("expected_output", "")
        if raw_expected.strip():
            try:
                expected = _csv_json(raw_expected)
            except ValueError:
                expected = raw_expected
        metadata: Any = {}
        raw_metadata = cells.get("metadata", "")
        if raw_metadata.strip():
            try:
                metadata = _csv_json(raw_metadata)
            except ValueError:
                raise CsvFormatError(row, "metadata must be the JSON text of an object") from None
            if not isinstance(metadata, dict):
                raise CsvFormatError(row, "metadata must be the JSON text of an object")
        try:
            case = EvalCase.model_validate(
                {"name": name, "input": cells["input"], "expected_output": expected, "metadata": metadata}
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise CsvFormatError(row, f"{where}: {first['msg']}" if where else first["msg"]) from None
        cases.append(
            {
                "name": case.name,
                "input": case.input,
                "expected_output": case.expected_output,
                "metadata": dict(case.metadata),
                "evaluators": [],
            }
        )
    return cases


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
    "CSV_COLUMNS",
    "CsvFormatError",
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
    "parse_csv_cases",
    "parse_file",
    "prescan_yaml",
    "read_dataset",
]
