import hashlib

import pytest

from eap_kb_sources import adapter_for, register_adapter
from eap_kb_sources.adapter import DocumentPolicy, document_key, secret_name
from eap_kb_sources.errors import ERROR_CODES, NEEDS_ATTENTION_CODES, SourceError, error_entry


def test_datasheet_key_is_deterministic_and_has_no_url_hash():
    key = document_key("src1", "product", "LM358", "datasheet", "https://x/a.pdf")
    assert key == "source:src1:product:LM358:datasheet"
    assert key == document_key("src1", "product", "LM358", "datasheet", "https://x/other.pdf")


def test_multi_document_relationships_append_the_url_hash():
    url = "https://www.ti.com/lit/an/slva1.pdf"
    key = document_key("src1", "product", "LM358", "application_note", url)
    assert key == f"source:src1:product:LM358:application_note:{hashlib.sha1(url.encode()).hexdigest()[:8]}"
    assert key != document_key("src1", "product", "LM358", "application_note", url + "?v=2")


def test_keys_are_one_url_segment_and_bounded():
    odd = document_key("src1", "product", "AB/C 1%0A", "datasheet")
    assert "/" not in odd and " " not in odd and "\n" not in odd and odd.startswith("source:src1:product:")
    assert odd != document_key("src1", "product", "AB/C 1%0B", "datasheet")
    long = document_key("src1", "product", "X" * 500, "application_note", "https://x/y")
    assert len(long) <= 256 and long.endswith(hashlib.sha1(b"https://x/y").hexdigest()[:8])
    assert document_key("src1", "product", "X" * 500, "datasheet") != document_key("src1", "product", "X" * 499 + "Y", "datasheet")


def test_secret_name_accepts_a_ref_or_a_bare_name():
    assert secret_name("secret:ti-key") == "ti-key" and secret_name("ti-key") == "ti-key"


def test_document_policy_defaults_parsing_and_cap():
    assert DocumentPolicy.from_json({}).types == ("datasheet",)
    policy = DocumentPolicy.from_json({"types": ["datasheet", "errata", 5], "max_bytes": 1000})
    assert policy.types == ("datasheet", "errata") and policy.max_bytes == 1000
    assert DocumentPolicy.from_json({"max_bytes": 10**12}).max_bytes == 25 * 1024 * 1024


def test_the_taxonomy_is_stable():
    assert {
        "connection_not_found", "connection_disabled", "secret_missing", "connection_auth_failed",
        "connection_unreachable", "connection_response_invalid", "source_adapter_not_found", "rate_limited",
        "not_found", "response_too_large", "url_not_allowed", "provider_error", "run_cancelled",
    } <= ERROR_CODES
    assert NEEDS_ATTENTION_CODES == {"connection_auth_failed", "secret_missing"}
    with pytest.raises(ValueError):
        SourceError("bogus")


def test_error_entries_are_bounded_and_never_quote_unexpected_text():
    assert error_entry("LM358", SourceError("not_found", "x" * 500)) == {
        "external_id": "LM358", "code": "not_found", "message": "x" * 200,
    }
    assert error_entry(None, RuntimeError("client_secret=abc")) == {"code": "provider_error", "message": "RuntimeError"}


def test_the_registry_is_keyed_by_kind_and_provider():
    sentinel = object()
    register_adapter("product_catalog", "reg-test", lambda: sentinel)
    assert adapter_for("product_catalog", "reg-test") is sentinel
    with pytest.raises(SourceError) as info:
        adapter_for("product_catalog", "nobody")
    assert info.value.code == "source_adapter_not_found"
