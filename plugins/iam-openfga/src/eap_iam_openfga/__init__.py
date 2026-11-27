"""`eap-iam-openfga`: OpenFGA as the platform's policy engine (design 5.4 to 5.6, P2).

One entry point, `eap.iam.policy` / `openfga`: the engine, the model, the bootstrap and the reconcile and backfill
tools. The services ask it (P2b) and Studio's Access tab writes grants through it (P2c) once it is selected; the deployed
policy stays `openaccess` until the operator switches (docs/iam.md, "Switching to OpenFGA")."""

from __future__ import annotations

import httpx2
from eap_iam import PluginContext

from eap_iam_openfga.client import OpenFgaClient
from eap_iam_openfga.engine import OpenFgaPolicy
from eap_iam_openfga.settings import load_settings


def create(ctx: PluginContext, *, http: httpx2.AsyncClient | None = None) -> OpenFgaPolicy:
    return OpenFgaPolicy(OpenFgaClient(load_settings(ctx.env), http))


__all__ = ["OpenFgaClient", "OpenFgaPolicy", "create"]
