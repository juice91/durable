"""`regex` negate/field: pydantic_evals dataset export and import round trip, save-time RE2 check."""

import pytest

from eap_api.eval_format import EvalFormatError, evaluator_from_spec, evaluator_to_spec
from eap_api.routes.evals import REGEX_INVALID, check_regex_evaluators
from eap_schemas.evals import EvalSuiteInvalid, Regex


def test_defaults_are_left_out_of_the_export():
    assert evaluator_to_spec(Regex(pattern="x")) == {"Regex": {"pattern": "x"}}


def test_negate_and_field_round_trip():
    original = Regex(pattern="https?://", negate=True, field="post", name="no_url")
    spec = evaluator_to_spec(original)
    assert spec == {"Regex": {"pattern": "https?://", "negate": True, "field": "post", "evaluation_name": "no_url"}}
    back = evaluator_from_spec(spec, "evaluators.0")
    assert Regex.model_validate(back) == original


def test_import_reads_the_short_form_and_rejects_a_bad_field():
    assert evaluator_from_spec({"Regex": "x"}, "p")["pattern"] == "x"
    with pytest.raises(EvalFormatError):
        evaluator_from_spec({"Regex": {"pattern": "x", "field": "a..b"}}, "p")


def test_save_time_re2_check_still_rejects_a_negated_pattern():
    with pytest.raises(EvalSuiteInvalid) as exc:
        check_regex_evaluators([{"kind": "regex", "pattern": "zq(", "negate": True, "field": "post"}], "evaluators")
    assert exc.value.reason == REGEX_INVALID
