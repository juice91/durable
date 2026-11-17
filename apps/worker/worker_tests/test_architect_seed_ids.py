"""The seed's Architect tool list (eap_schemas) is exactly what the worker registers (Task 5)."""

from eap_schemas.architect import ARCHITECT_GATED_TOOL_IDS, ARCHITECT_TOOL_IDS
from eap_worker import architect_tools


def test_seed_tool_ids_are_the_workers_tool_ids():
    assert architect_tools.TOOL_IDS == ARCHITECT_TOOL_IDS
    assert architect_tools.GATED_TOOL_IDS == ARCHITECT_GATED_TOOL_IDS
