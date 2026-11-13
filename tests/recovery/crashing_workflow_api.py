"""An API process that dies inside the workflow-run hand-off (scenario W6, test_workflow_crash.py).

Run as `python tests/recovery/crashing_workflow_api.py <mode> <tenant> ...` with the test's EAP_*
environment. It builds the real API app (`eap_api.app.create_app`) and sends one real request
through it; the only change is one launcher method replaced with `os._exit(137)` (no exception
handling, no cleanup, like `kill -9`):

- `run <workflow_id> <run_id> <input_json>`: POST /workflows/{id}/runs; dies in
  `DBOSRunLauncher.launch_workflow`, right after the route committed the kind='workflow' runs row.
- `approve <run_id> <step_id>`: POST /workflows/runs/{run_id}/steps/{step_id}/approve; dies in
  `DBOSRunLauncher.notify_approval`, right after the decision event committed and before the
  DBOS wake-up was sent.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    mode, tenant, *args = sys.argv[1:]
    os.environ["EAP_AUTH_MODE"] = "dev"

    from fastapi.testclient import TestClient

    from eap_api import launcher
    from eap_api.app import create_app

    def crash(self: object, **kwargs: object) -> None:
        print(f"EAP_API_CRASH {mode} launcher reached ({sorted(kwargs)}); exiting 137", flush=True)
        os._exit(137)

    if mode == "run":
        launcher.DBOSRunLauncher.launch_workflow = crash  # type: ignore[method-assign]
    elif mode == "approve":
        launcher.DBOSRunLauncher.notify_approval = crash  # type: ignore[method-assign]
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    app = create_app(database_url=os.environ["EAP_DATABASE_URL"])
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{tenant}:alice"}
    if mode == "run":
        workflow_id, run_id, input_json = args
        response = client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            json={"run_id": run_id, "input": json.loads(input_json)},
            headers=headers,
        )
    else:
        run_id, step_id = args
        response = client.post(
            f"/api/v1/workflows/runs/{run_id}/steps/{step_id}/approve", json={"comment": "lost wake-up"},
            headers=headers,
        )
    print(f"request returned without crashing: {response.status_code} {response.text}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
