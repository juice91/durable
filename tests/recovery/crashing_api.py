"""Scenario 4's API process: POST a run, die inside the launcher (after the runs row committed).

Run as `python tests/recovery/crashing_api.py <agent_id> <run_id> <input> <tenant>` with the same
EAP_* environment as the test. It builds the real API app (`eap_api.app.create_app`) and sends one
real POST through it; the only change is that `DBOSRunLauncher.launch`, the step right after the
route inserts the row, calls `os._exit(137)` (no exception handling, no cleanup, like `kill -9`).
The test then retries the same POST against a fresh API instance.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    agent_id, run_id, input_text, tenant = sys.argv[1:5]
    os.environ["EAP_AUTH_MODE"] = "dev"

    from fastapi.testclient import TestClient

    from eap_api import launcher
    from eap_api.app import create_app

    def crash(self: object, **kwargs: object) -> None:
        print(f"EAP_API_CRASH launcher reached for run {kwargs.get('run_id')}; exiting 137", flush=True)
        os._exit(137)

    launcher.DBOSRunLauncher.launch = crash  # type: ignore[method-assign]
    app = create_app(database_url=os.environ["EAP_DATABASE_URL"])
    client = TestClient(app)
    response = client.post(
        f"/api/v1/agents/{agent_id}/runs",
        json={"run_id": run_id, "input": input_text, "version": None},
        headers={"Authorization": f"Bearer dev:{tenant}:alice"},
    )
    print(f"POST returned without crashing: {response.status_code} {response.text}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
