"""`python -m eap_iam_openfga.email_subjects`: find and delete the tuples whose subject is an e-mail address.

The legacy `cfaccess` provider named a person by e-mail, and the backfill of that time gave the owner `owner` tuples
under the address. With `cfaccess` removed no identity provider produces such a subject, and a right keyed on an address
must not wait for one that does (tenant provisioning design, F1).

DRY-RUN BY DEFAULT: nothing is written without `--apply`. The summary has counts by tuple type, never an id or a
subject. A resource whose only owners are e-mail subjects would be left without an owner: `--apply` stops (exit 3)
unless `--allow-ownerless` says that is intended (tenant admins keep their rights; `reconcile`'s backfill can add the
account as owner first). Running it twice deletes nothing the second time."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import TextIO

import httpx2
from eap_iam import PolicyUnavailable

from eap_iam_openfga.client import OpenFgaClient, TupleKey
from eap_iam_openfga.names import is_email_subject, split_object, subject_of, tenant_ref
from eap_iam_openfga.reconcile import email_subjects_allowed
from eap_iam_openfga.settings import load_settings

EXIT_OWNERLESS = 3


def _kind(obj: str) -> str:
    return obj.partition(":")[0]


def _in_tenant(obj: str, tenant_id: str) -> bool:
    parts = split_object(obj)
    return parts[1] == tenant_id if parts else obj == tenant_ref(tenant_id)


def find(tuples: Sequence[TupleKey], tenant_id: str | None = None) -> tuple[list[TupleKey], list[str]]:
    """(the tuples with an e-mail subject, the objects that would be left without an owner once they are gone)."""
    found = [
        t for t in tuples
        if is_email_subject(subject_of(t.user)) and (tenant_id is None or _in_tenant(t.object, tenant_id))
    ]
    doomed = set(found)
    losing = {t.object for t in found if t.relation == "owner"}
    keeping = {t.object for t in tuples if t.relation == "owner" and t not in doomed}
    return sorted(found, key=lambda t: (t.object, t.relation, t.user)), sorted(losing - keeping)


def render(found: Sequence[TupleKey], ownerless: Sequence[str], *, applied: bool) -> str:
    lines = [
        f"e-mail subjects: {'applied' if applied else 'dry run (nothing written; pass --apply)'}",
        f"tuples with an e-mail subject: {len(found)}",
        *(f"  {name}: {count}" for name, count in sorted(Counter(f"{t.relation}:{_kind(t.object)}" for t in found).items())),
        f"distinct e-mail subjects: {len({t.user for t in found})}",
        f"resources that would be left without an owner: {len(ownerless)}",
        *(f"  {name}: {count}" for name, count in sorted(Counter(map(_kind, ownerless)).items())),
    ]
    if applied:
        lines.append(f"deleted: {len(found)}")
    return "\n".join(lines) + "\n"


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eap_iam_openfga.email_subjects", description=__doc__.split("\n\n")[0])
    p.add_argument("--tenant", default=None, help="only the objects of this tenant (default: the whole store)")
    p.add_argument("--apply", action="store_true", help="delete them (default: a dry run that only counts)")
    p.add_argument("--allow-ownerless", action="store_true", help="delete even when a resource is left without an owner")
    return p.parse_args(argv)


async def amain(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, out: TextIO | None = None,
    http: httpx2.AsyncClient | None = None,
) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    args = _args(argv)
    if args.apply and email_subjects_allowed(env):
        print(
            "refusing to delete: cfaccess is still in EAP_IAM_IDENTITY, so these subjects are live; remove it and "
            "deploy first",
            file=sys.stderr,
        )
        return 2
    try:
        settings = load_settings(env)
    except Exception as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    client = OpenFgaClient(settings, http)
    try:
        found, ownerless = find(await client.read(), args.tenant)
        if args.apply and ownerless and not args.allow_ownerless:
            out.write(render(found, ownerless, applied=False))
            out.write(
                "nothing deleted: give those resources another owner first (python -m eap_iam_openfga.backfill "
                "--owner zitadel:<id> --apply), or pass --allow-ownerless\n"
            )
            return EXIT_OWNERLESS
        if args.apply:
            await client.write((), found)
        out.write(render(found, ownerless, applied=args.apply))
        return 0
    except PolicyUnavailable as exc:
        print(f"failed: {exc.detail}", file=sys.stderr)
        return 1
    finally:
        if http is None:
            await client.aclose()


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
