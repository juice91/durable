"""Pytest plugin (entry point `pytest11`), active in every pytest session of the workspace.

- `EAP_IAM_DEVLOOPBACK_EXTRA_PEERS=testclient`: Starlette's TestClient reports the peer `testclient`, not an address,
  so the dev bearer needs it named as loopback. A real ASGI server always reports an IP; nothing outside pytest sets it.
- `EAP_IAM_TEST_FAKES=1`: run with every IAM plug-in absent and the fakes in place (`eap_iam.testing`): installed
  plug-ins are hidden and the identity provider `fake` plus the policy engine `fake` are selected.
"""

from __future__ import annotations

import os


def pytest_configure(config) -> None:  # noqa: ANN001
    os.environ.setdefault("EAP_IAM_DEVLOOPBACK_EXTRA_PEERS", "testclient")
    if os.environ.get("EAP_IAM_TEST_FAKES") == "1":
        from eap_iam.testing import register_fakes

        os.environ["EAP_IAM_IGNORE_PLUGINS"] = "apikey,cfaccess,devloopback,openaccess"
        os.environ["EAP_IAM_IDENTITY"] = "fake"
        os.environ["EAP_IAM_POLICY"] = "fake"
        register_fakes()
