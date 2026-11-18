"""`regex` evaluator: `negate` and `field` (defaults keep existing suites unchanged)."""

import pytest
from pydantic import ValidationError

from eap_schemas.evals import Regex


def test_defaults_keep_old_suites_unchanged():
    r = Regex.model_validate({"kind": "regex", "pattern": "x"})
    assert r.negate is False and r.field is None


def test_negate_and_field_are_accepted():
    r = Regex.model_validate({"kind": "regex", "pattern": "x", "negate": True, "field": "post.0"})
    assert r.negate is True and r.field == "post.0"


@pytest.mark.parametrize("bad", ["", "a..b", "a b", "a/b", ".a"])
def test_field_uses_the_field_equals_path_grammar(bad):
    with pytest.raises(ValidationError):
        Regex.model_validate({"kind": "regex", "pattern": "x", "field": bad})
