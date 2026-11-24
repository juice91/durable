"""`python -m eap_iam_openfga.backfill`: the one-off P2 backfill of tenant `studio` (design 11).

Writes, for every resource in the catalog, the link to its tenant, and gives the owner's account (`--owner-email`, or
`--owner` subjects) `owner` on all of them, and the creator recorded on each row when it is a real principal. Dry run by
default, idempotent. It is `eap_iam_openfga.reconcile` with owners turned on; see that module."""

from __future__ import annotations

import asyncio
import sys

from eap_iam_openfga.reconcile import amain


def main() -> None:
    sys.exit(asyncio.run(amain(backfill=True)))


if __name__ == "__main__":
    main()
