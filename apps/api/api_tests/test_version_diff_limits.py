"""Version compare stays fast on huge instructions: past the line limits the instructions diff is
one coarse block replace with `instructions_diff_truncated: true` (difflib is quadratic in lines)."""

from __future__ import annotations

import uuid

from eap_api.version_diff import MAX_DIFF_LINES, compare_specs, diff_too_large, instructions_diff

from .conftest import get_auth_header
from .test_versions_acceptance import _agent_with_versions, _spec
from .timing import best_of_3, cap


def test_small_diff_is_line_by_line_and_not_truncated():
    out = compare_specs(_spec("one\ntwo"), _spec("one\n2"))
    assert out["instructions_diff_truncated"] is False
    assert [d["op"] for d in out["instructions_diff"]] == ["equal", "removed", "added"]


def test_32k_line_pair_is_fast_and_coarse():
    before = "\n" * 32_000
    after = "x\n" + "\n" * 31_999
    out, elapsed = best_of_3(lambda: compare_specs(_spec(before), _spec(after)))
    assert elapsed < cap(1.0), elapsed
    assert out["instructions_diff_truncated"] is True
    ops = [d["op"] for d in out["instructions_diff"]]
    assert ops == ["removed"] * 32_000 + ["added"] * 32_000
    assert out["instructions_diff"][0] == {"op": "removed", "text": "", "base_line": 1, "target_line": None}
    assert out["instructions_diff"][32_000] == {"op": "added", "text": "x", "base_line": None, "target_line": 1}


def test_identical_huge_texts_are_all_equal():
    text = "a\n" * 5_000
    lines = instructions_diff(text, text)
    assert len(lines) == 5_000 and all(d["op"] == "equal" for d in lines)


def test_limits():
    assert not diff_too_large(MAX_DIFF_LINES, MAX_DIFF_LINES)  # 4e6 product, both at the limit
    assert diff_too_large(MAX_DIFF_LINES + 1, 1)
    assert diff_too_large(1, MAX_DIFF_LINES + 1)
    assert not diff_too_large(MAX_DIFF_LINES, 1)


def test_compare_route_with_32k_line_draft_is_fast(client):
    tenant = f"t{uuid.uuid4().hex[:10]}"
    h = get_auth_header(tenant, "alice")
    agent_id = _agent_with_versions(client, tenant, "\n" * 32_000)
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("y\n" * 16_000)}, headers=h).status_code == 200
    r, elapsed = best_of_3(
        lambda: client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=draft", headers=h)
    )
    assert r.status_code == 200
    assert elapsed < cap(2.0), elapsed
    body = r.json()
    assert body["instructions_diff_truncated"] is True
    assert {d["op"] for d in body["instructions_diff"]} == {"removed", "added"}
