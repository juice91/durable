"""ModelNotAllowed survives pickling (the worker raises it before model calls, inside DBOS steps)."""

from __future__ import annotations

import pickle

from eap_schemas.models_policy import ModelNotAllowed


def test_round_trip():
    exc = ModelNotAllowed("openai", "gpt-4")
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is ModelNotAllowed
    assert (restored.provider, restored.name, str(restored)) == ("openai", "gpt-4", str(exc))
    assert str(exc) == "model 'openai:gpt-4' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)"
