"""The reusable regex scorer takes `negate` and `field` (the same Regex contract as suites)."""

import pytest

from eap_schemas.scorers import ScorerInvalid, validate_scorer_evaluator


def test_scorer_keeps_negate_and_field():
    out = validate_scorer_evaluator({"kind": "regex", "pattern": "x", "negate": True, "field": "post.0"})
    assert out["negate"] is True and out["field"] == "post.0"


def test_scorer_without_them_defaults_off():
    out = validate_scorer_evaluator({"kind": "regex", "pattern": "x"})
    assert out["negate"] is False and out["field"] is None


def test_scorer_refuses_a_bad_field_path_naming_it():
    with pytest.raises(ScorerInvalid) as exc:
        validate_scorer_evaluator({"kind": "regex", "pattern": "x", "field": "a..b"})
    assert exc.value.path == "evaluator.field"
