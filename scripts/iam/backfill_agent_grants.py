#!/usr/bin/env python3
"""Migration report and apply for agent-bound grants (docs/iam.md, "Agent and workflow grants").

    uv run python scripts/iam/backfill_agent_grants.py --tenant studio                 # dry run: table
    uv run python scripts/iam/backfill_agent_grants.py --tenant studio --json report.json
    uv run python scripts/iam/backfill_agent_grants.py --tenant studio --apply --operator <you>

Reads `EAP_DATABASE_URL` and, for the current grants and for `--apply`, the policy engine of `EAP_IAM_POLICY` with its own
settings (`EAP_OPENFGA_*`). A dry run works with any engine (with `openaccess` every grant reads as to_grant)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from eap_iam import load_policy
from eap_run_store import AuditStore
from sqlalchemy import create_engine

from eap_api.services.agent_grants_backfill import apply_report, build_report, render_table
from eap_api.services.authz import AuthzService


async def amain(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tenant", required=True, help="the tenant to report on (every query is scoped to it)")
    p.add_argument("--apply", action="store_true", help="write the grants (default: a dry run that writes nothing)")
    p.add_argument("--operator", help="who applies it; recorded as `backfill:<operator>` in the audit log (required with --apply)")
    p.add_argument("--json", dest="json_path", help="also write the report as JSON to this file ('-' for stdout, then no table)")
    args = p.parse_args(argv)
    if args.apply and not args.operator:
        print("--apply needs --operator", file=sys.stderr)
        return 2
    url = os.environ.get("EAP_DATABASE_URL", "")
    if not url:
        print("EAP_DATABASE_URL is not set", file=sys.stderr)
        return 2
    db = create_engine(url)
    try:
        authz = AuthzService(load_policy(db))
        report = await build_report(db, authz, args.tenant)
        applied = None
        if args.apply:
            if not authz.supports_grants:
                print("the selected policy engine keeps no agent grants; set EAP_IAM_POLICY=openfga (and EAP_OPENFGA_*) to apply", file=sys.stderr)
                return 2
            applied = await apply_report(db, authz, args.tenant, report, args.operator, AuditStore(db))
            report = applied["report"]
        document = {**report, **({"applied": {k: v for k, v in applied.items() if k != "report"}} if applied else {})}
        if args.json_path == "-":
            print(json.dumps(document, indent=2, default=str))
        else:
            sys.stdout.write(render_table(report, applied=applied))
            if args.json_path:
                with open(args.json_path, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, indent=2, default=str)
        return 0 if applied is None or applied["verified"] else 1
    finally:
        db.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
