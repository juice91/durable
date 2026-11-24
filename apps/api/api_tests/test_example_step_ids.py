"""Every shipped example workflow follows the step-id convention (no `step_id_convention` warning at publish),
except the ones listed with a reason. Pure: no database."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from eap_run_store.workflow_checks import step_id_convention_warnings
from eap_schemas.workflows import WorkflowSpec

EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "workflows"

# The spec of the published "KB Source Synchronization" workflow, which the nightly schedule runs and Studio
# publishes from this file. Renaming its step ids changes the durable step layout, so it is left for a
# deliberate migration; it still warns.
LEGACY_IDS = {"kb-source-sync.json"}

AGENT = "00000000-0000-0000-0000-000000000001"
EXAMPLES = sorted(p.name for p in EXAMPLES_DIR.glob("*.json"))


def test_the_examples_are_found():
    assert len(EXAMPLES) >= 6 and LEGACY_IDS <= set(EXAMPLES)


def _spec(name: str) -> WorkflowSpec:
    text = (EXAMPLES_DIR / name).read_text(encoding="utf-8")
    return WorkflowSpec.model_validate(json.loads(re.sub(r"\$\{agents\.[a-z0-9_-]+\}", AGENT, text))["spec"])


@pytest.mark.parametrize("name", [n for n in EXAMPLES if n not in LEGACY_IDS])
def test_an_example_has_no_step_id_convention_warning(name):
    assert step_id_convention_warnings(_spec(name)) == []


def test_the_legacy_example_still_warns_so_the_exemption_is_not_forgotten():
    assert step_id_convention_warnings(_spec("kb-source-sync.json"))
