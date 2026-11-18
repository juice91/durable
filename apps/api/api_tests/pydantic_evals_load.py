"""Load an exported suite file with the real pydantic_evals (2.67.0) Dataset loader.

Run OUTSIDE the project environment (the API never imports pydantic_evals), by
test_evals_api.py::test_export_loads_with_real_pydantic_evals:

    uv run --no-project --offline --with pydantic-evals==2.67.0 python pydantic_evals_load.py FILE

It defines our custom evaluator classes exactly as eap_api.eval_format's docstring describes and
prints one JSON line: the loaded evaluators per case and for the dataset, as `[class, {args}]`.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from dataclasses import dataclass
from typing import Any

from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext


@dataclass
class Regex(Evaluator):
    pattern: str
    negate: bool = False
    field: str | None = None
    evaluation_name: str | None = None

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return True


@dataclass
class SchemaValid(Evaluator):
    json_schema: Any = None
    evaluation_name: str | None = None

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return True


@dataclass
class FieldEquals(Evaluator):
    path: str
    value: Any
    evaluation_name: str | None = None

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return True


@dataclass
class MaxCost(Evaluator):
    usd: Any
    evaluation_name: str | None = None

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return True


@dataclass
class ContainsExpected(Evaluator):
    case_sensitive: bool = True
    evaluation_name: str | None = None

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return True


@dataclass
class NamedMaxDuration(Evaluator):
    seconds: float
    evaluation_name: str | None = None

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return True


@dataclass
class ThresholdLLMJudge(Evaluator):
    rubric: str
    model: str
    include_input: bool = False
    include_expected_output: bool = False
    assertion: Any = None
    pass_threshold: float | None = None

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return True


CUSTOM = [Regex, SchemaValid, FieldEquals, MaxCost, ContainsExpected, NamedMaxDuration, ThresholdLLMJudge]


def _describe(evaluator: Any) -> list[Any]:
    args = {f.name: getattr(evaluator, f.name) for f in dataclasses.fields(evaluator)}
    return [type(evaluator).__name__, json.loads(json.dumps(args, default=str))]


def main(path: str) -> None:
    fmt = "json" if path.endswith(".json") else "yaml"
    with open(path, encoding="utf-8") as f:
        text = f.read()
    dataset = Dataset[str, Any, dict].from_text(text, fmt=fmt, custom_evaluator_types=CUSTOM)
    print(json.dumps({
        "name": dataset.name,
        "evaluators": [_describe(e) for e in dataset.evaluators],
        "cases": [{"name": c.name, "inputs": c.inputs, "evaluators": [_describe(e) for e in c.evaluators]}
                  for c in dataset.cases],
    }))


if __name__ == "__main__":
    main(sys.argv[1])
