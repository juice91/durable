"""Where the model file is, and what it defines (the relations the engine may ask for)."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from eap_iam_openfga.dsl import parse_model, relations_of

MODEL_FILE = "durable.fga"
# A checkout keeps the file in plugins/iam-openfga/model/; the wheel carries a copy next to this module.
_SOURCE_TREE = Path(__file__).resolve().parents[2] / "model" / MODEL_FILE


def model_path() -> Path:
    if _SOURCE_TREE.is_file():
        return _SOURCE_TREE
    return Path(str(resources.files("eap_iam_openfga").joinpath(MODEL_FILE)))


def model_text() -> str:
    return model_path().read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def model_json() -> dict[str, Any]:
    return parse_model(model_text())


@lru_cache(maxsize=1)
def model_relations() -> dict[str, frozenset[str]]:
    return relations_of(model_json())
