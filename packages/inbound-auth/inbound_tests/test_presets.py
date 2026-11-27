"""eap_inbound: every preset round-trips, the documented vectors, tolerance boundaries, constant-time evaluation."""

import base64
import itertools

import pytest

from eap_inbound import CustomConfig, dedupe_key, verify
from eap_inbound import presets
from eap_inbound.signing import custom_headers, github_headers, standard_webhooks_headers

NOW = 1_700_000_000
WHSEC = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
WHSEC2 = "whsec_" + base64.b64encode(b"fedcba9876543210fedcba9876543210").decode()


def test_github_documented_vector():
    # GitHub's documentation: secret "It's a Secret to Everybody", body "Hello, World!".
    headers = {"X-Hub-Signature-256": "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"}
    got = verify("github", None, headers, b"Hello, World!", ["It's a Secret to Everybody"], NOW)
    assert got.ok and got.secret_slot == 0 and got.timestamp is None


def test_github_failures_are_distinguished_for_counters_only():
    body = b'{"a":1}'
    good = github_headers("s3cret", body)
    assert verify("github", None, good, body, ["s3cret"], NOW).ok
    assert verify("github", None, good, body + b" ", ["s3cret"], NOW).reason == "signature_mismatch"
    assert verify("github", None, good, body, ["other"], NOW).reason == "signature_mismatch"
    assert verify("github", None, {}, body, ["s3cret"], NOW).reason == "missing_header"
    assert verify("github", None, {"x-hub-signature-256": "sha1=abcd"}, body, ["s3cret"], NOW).reason == "malformed_signature"
    assert verify("github", None, {"x-hub-signature": good["x-hub-signature-256"]}, body, ["s3cret"], NOW).reason == "missing_header"
    assert verify("github", None, good, body, [None], NOW).reason == "secret_missing"


def test_github_second_secret_matches_and_names_its_slot():
    body = b"{}"
    headers = github_headers("new", body)
    got = verify("github", None, headers, body, ["old", "new"], NOW)
    assert got.ok and got.secret_slot == 1
    assert verify("github", None, headers, body, [None, "new"], NOW).secret_slot == 1


def test_header_names_are_case_insensitive():
    body = b"{}"
    headers = {k.upper(): v for k, v in github_headers("s", body).items()}
    assert verify("github", None, headers, body, ["s"], NOW).ok


def test_standard_webhooks_round_trip_and_window():
    body = b'{"type":"x"}'
    headers = standard_webhooks_headers(WHSEC, body, msg_id="msg_1", timestamp=NOW)
    got = verify("standard_webhooks", None, headers, body, [WHSEC], NOW)
    assert got.ok and got.signed_id == "msg_1" and got.timestamp == NOW
    # exactly at the tolerance passes, one second past does not (in either direction)
    assert verify("standard_webhooks", None, headers, body, [WHSEC], NOW + 300, 300).ok
    stale = verify("standard_webhooks", None, headers, body, [WHSEC], NOW + 301, 300)
    assert stale.signature_ok and stale.reason == "timestamp_stale" and not stale.ok
    future = verify("standard_webhooks", None, headers, body, [WHSEC], NOW - 301, 300)
    assert future.signature_ok and future.reason == "timestamp_stale"


def test_standard_webhooks_stale_with_wrong_signature_is_a_plain_mismatch():
    body = b"{}"
    headers = standard_webhooks_headers(WHSEC, body, msg_id="m", timestamp=NOW - 10_000)
    got = verify("standard_webhooks", None, headers, body, [WHSEC2], NOW)
    assert not got.signature_ok and got.reason == "signature_mismatch"


def test_standard_webhooks_rolled_secret_and_junk_entries():
    body = b"{}"
    headers = standard_webhooks_headers(WHSEC2, body, msg_id="m", timestamp=NOW)
    good = headers["webhook-signature"]
    headers["webhook-signature"] = f"v1,AAAA v1a,BBBB v0,CCCC {good}"
    assert verify("standard_webhooks", None, headers, body, [WHSEC, WHSEC2], NOW).secret_slot == 1
    headers["webhook-signature"] = " ".join(["v1,junk"] * 8 + [good])
    assert verify("standard_webhooks", None, headers, body, [WHSEC2], NOW).reason == "malformed_signature"
    headers["webhook-signature"] = "v1a,BBBB"
    assert verify("standard_webhooks", None, headers, body, [WHSEC2], NOW).reason == "malformed_signature"


def test_standard_webhooks_bad_secret_never_matches_and_is_reported():
    body = b"{}"
    headers = standard_webhooks_headers(WHSEC, body, msg_id="m", timestamp=NOW)
    assert verify("standard_webhooks", None, headers, body, ["not-whsec"], NOW).reason == "secret_invalid"
    assert verify("standard_webhooks", None, {**headers, "webhook-timestamp": "x"}, body, [WHSEC], NOW).reason == "malformed_signature"


@pytest.mark.parametrize(
    "template,algorithm,encoding,key_encoding",
    list(itertools.product(presets.SIGNED_PAYLOADS, ["sha256", "sha512"], ["hex", "base64"], ["utf8", "base64", "hex"])),
)
def test_custom_round_trips_every_combination(template, algorithm, encoding, key_encoding):
    config = CustomConfig(
        header="x-sig", prefix="v1=", algorithm=algorithm, encoding=encoding, key_encoding=key_encoding,
        timestamp={"header": "x-ts"}, id={"header": "x-id"}, signed_payload=template,
    )
    secret = {"utf8": "plain secret", "base64": base64.b64encode(b"binary key").decode(), "hex": "00ff10ab"}[key_encoding]
    body = "héllo ✓".encode()
    headers = custom_headers(config, secret, body, timestamp=NOW, msg_id="id-1")
    got = verify("custom", config, headers, body, [secret], NOW)
    assert got.ok and got.timestamp == NOW
    assert (got.signed_id == "id-1") == ("{id}" in template)
    assert not verify("custom", config, headers, body + b"x", [secret], NOW).ok


def test_custom_milliseconds_and_generic_sha256_prefix():
    config = CustomConfig(timestamp={"header": "x-ts", "unit": "milliseconds"}, signed_payload="{timestamp}.{body}")
    headers = custom_headers(config, "k", b"{}", timestamp=NOW)
    assert headers["x-ts"] == str(NOW * 1000)
    assert verify("custom", config, headers, b"{}", ["k"], NOW).ok
    body_only = CustomConfig(allow_body_hash_dedupe_only=True)
    headers = custom_headers(body_only, "k", b"{}")
    assert verify("custom", body_only, headers, b"{}", ["k"], NOW).ok
    assert headers["x-signature-256"].startswith("sha256=")


def test_custom_config_refuses_unsafe_shapes():
    with pytest.raises(ValueError, match="replay_protection_required"):
        CustomConfig()  # body only, no timestamp, no id, no explicit acceptance
    with pytest.raises(ValueError):
        CustomConfig(signed_payload="{timestamp}.{body}", allow_body_hash_dedupe_only=True)  # needs a timestamp header
    with pytest.raises(ValueError):
        CustomConfig(algorithm="sha1", allow_body_hash_dedupe_only=True)
    with pytest.raises(ValueError):
        CustomConfig(signed_payload="{body}{body}", allow_body_hash_dedupe_only=True)  # no free-form templates
    with pytest.raises(ValueError):
        CustomConfig(header="bad header", allow_body_hash_dedupe_only=True)


def test_custom_body_cannot_inject_placeholders():
    config = CustomConfig(timestamp={"header": "x-ts"}, signed_payload="{timestamp}.{body}")
    body = b"{timestamp} {id} {body}"
    headers = custom_headers(config, "k", body, timestamp=NOW)
    assert verify("custom", config, headers, body, ["k"], NOW).ok


def test_every_secret_and_candidate_is_compared_with_no_early_exit(monkeypatch):
    calls = []
    real = presets._compare

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(presets, "_compare", counting)
    body = b"{}"
    headers = standard_webhooks_headers(WHSEC, body, msg_id="m", timestamp=NOW)
    sig = headers["webhook-signature"]
    headers["webhook-signature"] = f"{sig} v1,aaaa v1,bbbb"  # the first candidate matches the first secret
    assert verify("standard_webhooks", None, headers, body, [WHSEC, WHSEC2], NOW).ok
    assert len(calls) == 2 * 3  # 2 secrets x 3 candidates, even though the very first pair matched
    assert presets.constant_time_equals(b"a", b"a") and not presets.constant_time_equals(b"a", b"b")


def test_empty_and_non_ascii_bodies():
    for body in (b"", "日本語".encode(), b"\x00\xff"):
        assert verify("github", None, github_headers("s", body), body, ["s"], NOW).ok


def test_dedupe_keys_use_signed_material_only():
    body = b'{"x":1}'
    gh = verify("github", None, github_headers("s", body), body, ["s"], NOW)
    key, kind = dedupe_key("github", gh, body)
    assert kind == "body" and key.startswith("body:")
    # a fresh X-GitHub-Delivery header changes nothing: the key is the body hash
    h2 = {**github_headers("s", body), "x-github-delivery": "other-guid"}
    assert dedupe_key("github", verify("github", None, h2, body, ["s"], NOW), body) == (key, kind)
    sw = verify("standard_webhooks", None, standard_webhooks_headers(WHSEC, body, msg_id="msg_9", timestamp=NOW), body, [WHSEC], NOW)
    assert dedupe_key("standard_webhooks", sw, body) == ("id:msg_9", "id")
    # a retry carries a new timestamp and signature but the same id: same key
    sw2 = verify("standard_webhooks", None, standard_webhooks_headers(WHSEC, body, msg_id="msg_9", timestamp=NOW + 5), body, [WHSEC], NOW + 5)
    assert dedupe_key("standard_webhooks", sw2, body) == ("id:msg_9", "id")
    long_id = "x" * 500
    sw3 = verify("standard_webhooks", None, standard_webhooks_headers(WHSEC, body, msg_id=long_id, timestamp=NOW), body, [WHSEC], NOW)
    assert len(dedupe_key("standard_webhooks", sw3, body)[0]) <= 200


def test_custom_unsigned_id_header_is_not_trusted_for_dedupe():
    config = CustomConfig(timestamp={"header": "x-ts"}, id={"header": "x-id"}, signed_payload="{timestamp}.{body}")
    headers = custom_headers(config, "k", b"{}", timestamp=NOW, msg_id="abc")
    got = verify("custom", config, headers, b"{}", ["k"], NOW)
    assert got.ok and got.signed_id is None
    assert dedupe_key("custom", got, b"{}")[1] == "body"
