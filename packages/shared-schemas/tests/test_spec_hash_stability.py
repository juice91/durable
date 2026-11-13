"""canonical_spec_hash must not change for any spec stored before `mcp_tools` existed (task 3.5).

Stored AgentVersion rows are validated by recomputing the hash, so a changed hash would make every
existing version unreadable. The literals below were computed before AgentSpec gained `mcp_tools`.
"""

from __future__ import annotations

import hashlib
import json

from eap_schemas import AgentSpec, AgentVersion, canonical_spec_hash

FULL = {
    "name": "Support triage",
    "instructions": "Open a ticket.",
    "model": {"provider": "test", "name": "ticket"},
    "tools": ["tickets.create", "clock.now"],
    "model_settings": {"temperature": 0.2},
}
MINIMAL = {"name": "Minimal", "model": {"provider": "test", "name": "echo"}}

# Computed on the pre-change code (commit 349a2d6).
FULL_HASH = "6a08ca8b955f04c20ebb3250e02563ace304ab65f997d56d094c4b82a1b8ee71"
MINIMAL_HASH = "a353a875819982f6ad02d6ae7bc416cd573402ed78c8a84e32211db1dacc3a1c"

# The JSON shape AgentSpec dumped before task 3.5 (every field, no mcp_tools).
PRE_CHANGE_FULL_JSON = {
    "spec_version": 1,
    "name": "Support triage",
    "instructions": "Open a ticket.",
    "model": {"provider": "test", "name": "ticket"},
    "tools": ["tickets.create", "clock.now"],
    "model_settings": {"temperature": 0.2, "max_tokens": None},
}


def _sha(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_hash_of_specs_without_mcp_tools_is_unchanged():
    assert canonical_spec_hash(AgentSpec.model_validate(FULL)) == FULL_HASH
    assert canonical_spec_hash(AgentSpec.model_validate(MINIMAL)) == MINIMAL_HASH
    assert _sha(PRE_CHANGE_FULL_JSON) == FULL_HASH


def test_explicit_empty_mcp_tools_hashes_and_dumps_like_absent():
    spec = AgentSpec.model_validate({**FULL, "mcp_tools": []})
    assert canonical_spec_hash(spec) == FULL_HASH
    assert spec.model_dump(mode="json") == PRE_CHANGE_FULL_JSON
    assert json.loads(spec.model_dump_json()) == PRE_CHANGE_FULL_JSON


def test_stored_version_without_mcp_tools_still_validates():
    AgentVersion.model_validate({
        "agent_id": "00000000-0000-0000-0000-000000000001",
        "tenant_id": "t1",
        "version": 1,
        "spec": PRE_CHANGE_FULL_JSON,
        "spec_hash": FULL_HASH,
        "created_at": "2026-11-01T00:00:00+00:00",
    })


def test_non_empty_mcp_tools_is_part_of_the_hash():
    spec = AgentSpec.model_validate({**FULL, "mcp_tools": [{"server": "docs", "tool": "search"}]})
    assert spec.model_dump(mode="json")["mcp_tools"] == [{"server": "docs", "tool": "search"}]
    assert canonical_spec_hash(spec) != FULL_HASH
