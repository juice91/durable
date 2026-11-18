"""Resolving `{"$template": ...}` input values (eap_worker.workflow_eval): pure."""

import pytest

from eap_worker.workflow_eval import RefMissing, resolve_template

CONTEXT = {
    "input": {"text": "hello", "langs": ["zh-Hant"], "n": 3, "flag": False},
    "steps": {"s": {"output": {"token": "tok-1", "did": "did:plc:abc", "obj": {"a": 1}, "nothing": None}}},
}


def test_a_whole_placeholder_keeps_the_type_and_a_mixed_string_concatenates():
    out = resolve_template({
        "headers": {"$template": {"Authorization": "Bearer {{ steps.s.output.token }}"}},
        "json": {"$template": {
            "repo": "{{steps.s.output.did}}", "record": {"text": "{{input.text}}", "langs": "{{input.langs}}"},
            "obj": "{{steps.s.output.obj}}", "msg": "n={{input.n}} flag={{input.flag}} {{steps.s.output.token}}!",
            "list": ["{{input.n}}", "x"], "plain": 7,
        }},
        "ref": {"$ref": "input.text"}, "lit": {"a": [1, {"$not": "a template"}]},
    }, CONTEXT)
    assert out == {
        "headers": {"Authorization": "Bearer tok-1"},
        "json": {"repo": "did:plc:abc", "record": {"text": "hello", "langs": ["zh-Hant"]}, "obj": {"a": 1},
                 "msg": "n=3 flag=false tok-1!", "list": [3, "x"], "plain": 7},
        "ref": "hello", "lit": {"a": [1, {"$not": "a template"}]},
    }


def test_a_bare_string_template_and_literal_braces_free_text():
    assert resolve_template({"a": {"$template": "Bearer {{steps.s.output.token}}"}, "b": {"$template": "no refs"}},
                            CONTEXT) == {"a": "Bearer tok-1", "b": "no refs"}


@pytest.mark.parametrize("placeholder", ["{{steps.s.output.missing}}", "{{steps.zz.output}}", "{{input.nope}}",
                                         "x {{steps.s.output.obj}}", "x {{steps.s.output.nothing}}", "{{input.langs}} y"])
def test_a_placeholder_that_does_not_resolve_or_is_not_a_scalar_inside_text_is_ref_missing(placeholder):
    with pytest.raises(RefMissing):
        resolve_template({"a": {"$template": {"k": [placeholder]}}}, CONTEXT)


def test_a_resolved_value_is_never_rescanned():
    context = {"input": {"x": "{{steps.s.output.token}}"}, "steps": {"s": {"output": {"token": "T"}}}}
    assert resolve_template({"a": {"$template": "v={{input.x}}"}}, context) == {"a": "v={{steps.s.output.token}}"}


def test_recorded_input_masks_sensitive_headers_only():
    from eap_secrets.redaction import REDACTED
    from eap_worker.workflow_eval import mask_recorded_input

    live = {"method": "POST", "headers": {
        "Authorization": "Bearer abc", "COOKIE": "s=1", "Proxy-Authorization": "x", "X-Api-Key": "k",
        "X-Auth-Token": "t", "my-Secret-thing": "s", "Content-Type": "application/json", "Accept": "*/*"}}
    masked = mask_recorded_input(live)
    assert masked["headers"] == {
        "Authorization": REDACTED, "COOKIE": REDACTED, "Proxy-Authorization": REDACTED, "X-Api-Key": REDACTED,
        "X-Auth-Token": REDACTED, "my-Secret-thing": REDACTED, "Content-Type": "application/json", "Accept": "*/*"}
    assert live["headers"]["Authorization"] == "Bearer abc"  # the live input is untouched
    plain = {"headers": {"Accept": "*/*"}, "x": 1}
    assert mask_recorded_input(plain) is plain
    assert mask_recorded_input({"x": 1}) == {"x": 1} and mask_recorded_input({"headers": "str"}) == {"headers": "str"}
