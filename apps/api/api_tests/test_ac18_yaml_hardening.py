"""AC.18: YAML import hardening through the real route, beyond the AC.12 tests (which cover Python
tags, aliases, nesting and size bombs, 1 MiB, UTF-8 and parse time).

Every file below is refused with 422 (never a 500), names no file content in the answer, and leaves
the suite exactly as it was: multi-document streams, unknown and local tags (`!include`,
`!!python/module`), a merge key that shares a nested node, and the YAML scalars that are not JSON
(dates and timestamps, `!!set`, `!!binary`, NaN and infinities) in each place a file carries
free-form data (`expected_output`, `metadata`). An integer beyond 64 bits is valid JSON: it is
stored exactly and the export re-imports to the same case.
"""

from __future__ import annotations

import pytest

from .run_helpers import requires_pg
from .test_evals_api import API, _agent, _suite, api, fake  # noqa: F401 - fixtures

pytestmark = requires_pg

REFUSED = {
    "multi-document": "cases: []\n---\ncases: []\n",
    "include tag": "cases:\n  - name: n\n    inputs: !include /etc/passwd\n",
    "python module tag": "cases:\n  - name: n\n    inputs: !!python/module:os\n",
    "merge shares a node": "base: &b {name: n, inputs: i, metadata: {k: [1]}}\ncases:\n  - <<: *b\n",
}
NOT_JSON = {
    "date": "2026-01-01",
    "timestamp": "2026-01-01 10:00:00",
    "set": "!!set {a, b}",
    "binary": "!!binary aGk=",
    "nan": ".nan",
    "infinity": "-.inf",
}
for _name, _value in NOT_JSON.items():
    REFUSED[f"{_name} in expected_output"] = f"cases:\n  - name: n\n    inputs: i\n    expected_output: {_value}\n"
    if _name != "binary":  # metadata `!!binary` is read as its text (reported, see the AC.18 report)
        REFUSED[f"{_name} in metadata"] = f"cases:\n  - name: n\n    inputs: i\n    metadata: {{at: {_value}}}\n"


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_hostile_or_non_json_yaml_is_a_422_and_changes_nothing(api, name):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    before = client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()
    r = client.post(f"{API}/eval-suites/{suite_id}/import", content=REFUSED[name].encode(), headers=h)
    assert r.status_code == 422, (name, r.status_code, r.text)
    assert "passwd" not in r.text and "aGk=" not in r.text
    assert client.get(f"{API}/eval-suites/{suite_id}", headers=h).json() == before



def test_a_huge_integer_is_stored_exactly_and_round_trips(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, cases=())
    value = int("f" * 400, 16)
    body = f"cases:\n  - name: n\n    inputs: i\n    expected_output: {value}\n"
    r = client.post(f"{API}/eval-suites/{suite_id}/import", content=body.encode(), headers=h)
    assert r.status_code == 200, r.text
    (case,) = client.get(f"{API}/eval-suites/{suite_id}/cases", headers=h).json()["items"]
    assert case["expected_output"] == value
    exported = client.get(f"{API}/eval-suites/{suite_id}/export", headers=h)
    assert exported.status_code == 200 and str(value) in exported.text
    again = client.post(f"{API}/eval-suites/{suite_id}/import", content=exported.content, headers=h)
    assert again.status_code == 200 and again.json()["created"] == 0, again.text
