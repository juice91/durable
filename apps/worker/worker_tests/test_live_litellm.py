"""Optional: one real agent run against the LiteLLM gateway (qwen3.8-thinking).

Skipped unless EAP_LITELLM_API_KEY or LITELLM_API_KEY is set. The base URL comes from
EAP_LITELLM_BASE_URL / OPENAI_BASE_URL (default: the local SSH tunnel).
"""

from __future__ import annotations

import os

import pytest

from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal

HAS_KEY = bool(os.environ.get("EAP_LITELLM_API_KEY") or os.environ.get("LITELLM_API_KEY"))

pytestmark = pytest.mark.skipif(not HAS_KEY, reason="no EAP_LITELLM_API_KEY / LITELLM_API_KEY set")


def test_live_qwen_one_turn(worker, client, tenant, monkeypatch):
    from dataclasses import replace

    from eap_worker.config import WorkerSettings

    env = WorkerSettings.from_env()
    base_url = env.litellm_base_url or "http://127.0.0.1:14001/v1"
    monkeypatch.setattr(worker, "settings", replace(worker.settings, litellm_base_url=base_url,
                                                     litellm_api_key=env.litellm_api_key))
    spec = ticket_spec(tools=())
    spec["model"] = {"provider": "litellm", "name": "qwen3.8-thinking"}
    spec["instructions"] = "Answer in one short sentence."
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec,
                         input_text="What is the capital of France?", app_version=APP_VERSION)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=280)
    assert run["status"] == "completed", run["error"]
    events = event_rows(worker.engine, run_id)
    assert [e[0] for e in events] == list(range(len(events)))
    assert [e[1] for e in events] == ["start", "model:0:final", "end"]
    assert events[1][2] == "model_message"
    assert run["output"]
