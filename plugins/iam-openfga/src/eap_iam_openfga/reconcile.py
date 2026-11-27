"""Reconcile and backfill: make OpenFGA hold the links the platform's own data implies (design 5.6, 11).

What is computed, from the resource catalog (`eap_run_store`, never ad-hoc SQL here):

  * a **link** `tenant:<T> tenant <kind>:<T>/<id>` for every resource, so it exists for the model at all. A missing
    link makes a resource invisible (fail closed), which is what the hourly reconcile repairs;
  * with `owners`, an **owner** tuple for each owner subject on every resource (the backfill of tenant `studio`: the
    owner's account holds `owner` on everything), plus the creator recorded on the row when it is a real principal
    (`zitadel:<sub>`); a legacy creator (`operator`, `dev:<user>`, ...) never becomes a principal. An e-mail address is
    a principal only while the legacy `cfaccess` provider is in `EAP_IAM_IDENTITY`: no other provider names a person by
    e-mail, so no right may be keyed on one (`python -m eap_iam_openfga.email_subjects` removes the old ones).

What is never touched: grants (`runner`, `approver`, `editor`, `legacy_usable_by`, `granted_to`) and any owner tuple an admin revoked on
purpose are not rewritten by the link reconcile; owners are only ever *added*, and only by an explicit backfill. Tenant
roles are not stored at all: they arrive as contextual tuples on every request, so a stored role could outlive its
removal. Runs are registered when they start and are not part of the catalog.

DRY-RUN BY DEFAULT: nothing is written without `--apply`. `--prune` also deletes the tuples of an object the catalog no
longer lists (a resource deleted while the engine was down). The summary has counts by tuple type, never an id."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TextIO

import httpx2
from eap_iam import CatalogEntry, GrantError, MembershipSource, PluginContext, PolicyUnavailable, ResourceCatalog, ResourceRef
from eap_iam.protocols import GROUP_MEMBERSHIP
from eap_iam.registry import installed, parse_names

from eap_iam_openfga.client import OpenFgaClient, TupleKey
from eap_iam_openfga.names import (
    REGISTERED_KINDS,
    is_email_subject,
    is_principal_subject,
    object_ref,
    split_object,
    tenant_ref,
    user_ref,
)
from eap_iam_openfga.settings import load_settings

log = logging.getLogger("eap.iam.openfga.reconcile")

SUMMARY_ORDER = ("link", "owner")


def email_subjects_allowed(env: Mapping[str, str]) -> bool:
    """Whether this deployment still names people by e-mail: only the legacy, single-tenant `cfaccess` provider does."""
    return "cfaccess" in parse_names(env.get("EAP_IAM_IDENTITY"))


def _label(t: TupleKey) -> str:
    """A tuple's type for the summary: `link:agent`, `owner:secret`. Never an id."""
    parts = split_object(t.object)
    kind = parts[0] if parts else t.object.partition(":")[0]
    return f"{'link' if t.relation == 'tenant' else t.relation}:{kind}"


@dataclass
class Plan:
    desired: set[TupleKey] = field(default_factory=set)
    present: set[TupleKey] = field(default_factory=set)
    to_add: list[TupleKey] = field(default_factory=list)
    orphans: list[TupleKey] = field(default_factory=list)  # tuples of objects the catalog no longer lists
    skipped_creators: int = 0  # rows whose recorded creator is a legacy value
    skipped_email_creators: int = 0  # rows whose creator is an e-mail address while `cfaccess` is not configured
    skipped_entries: int = 0  # rows with an id OpenFGA cannot hold

    def summary(self) -> dict[str, dict[str, int]]:
        return {
            "desired": dict(Counter(map(_label, self.desired))),
            "to_add": dict(Counter(map(_label, self.to_add))),
            "orphans": dict(Counter(map(_label, self.orphans))),
        }


def _object(entry: CatalogEntry) -> str | None:
    """The row's OpenFGA object, or None for a row the engine cannot hold."""
    if entry.kind not in REGISTERED_KINDS:
        return None
    try:
        return object_ref(ResourceRef(entry.kind, entry.id, entry.tenant_id))
    except GrantError:
        return None


def desired_tuples(
    entries: Iterable[CatalogEntry], owners: Sequence[str] = (), *, creators: bool = False, email_subjects: bool = False,
) -> tuple[set[TupleKey], int, int]:
    """(tuples, rows with a legacy creator, rows skipped). Owner tuples only when `owners` or `creators` asks for them;
    an e-mail creator becomes an owner only with `email_subjects` (`email_creators` counts the others)."""
    out: set[TupleKey] = set()
    legacy = skipped = 0
    for e in entries:
        obj = _object(e)
        if obj is None:
            skipped += 1
            continue
        out.add(TupleKey(tenant_ref(e.tenant_id), "tenant", obj))
        for subject in owners:
            out.add(TupleKey(user_ref(subject), "owner", obj))
        if creators and e.creator is not None:
            if not is_principal_subject(e.creator):
                legacy += 1
            elif email_subjects or not is_email_subject(e.creator):
                out.add(TupleKey(user_ref(e.creator), "owner", obj))
    return out, legacy, skipped


def email_creators(entries: Iterable[CatalogEntry]) -> int:
    return sum(1 for e in entries if _object(e) is not None and is_email_subject(e.creator))


async def build_plan(
    client: OpenFgaClient, catalog: ResourceCatalog, *, tenant_id: str | None = None, owners: Sequence[str] = (),
    creators: bool = False, email_subjects: bool = False,
) -> Plan:
    entries = list(catalog.entries(tenant_id))
    plan = Plan()
    plan.desired, plan.skipped_creators, plan.skipped_entries = desired_tuples(
        entries, owners, creators=creators, email_subjects=email_subjects
    )
    if creators and not email_subjects:
        plan.skipped_email_creators = email_creators(entries)
    tenants = {e.tenant_id for e in entries}
    if tenant_id:
        tenants.add(tenant_id)
    listed = {obj for e in entries if (obj := _object(e)) is not None}
    scoped_kinds = set(REGISTERED_KINDS)
    for t in await client.read():
        parts = split_object(t.object)
        if parts is None or parts[0] not in scoped_kinds or parts[1] not in tenants:
            continue  # another tenant, a run, a type this tool does not own
        plan.present.add(t)
        if t.object not in listed:
            plan.orphans.append(t)
    plan.to_add = sorted(plan.desired - plan.present, key=lambda t: (t.object, t.relation, t.user))
    return plan


async def apply_plan(client: OpenFgaClient, plan: Plan, *, prune: bool) -> None:
    await client.write(plan.to_add, plan.orphans if prune else ())


def render(plan: Plan, *, applied: bool, prune: bool, tenants: int) -> str:
    verb = "applied" if applied else "dry run (nothing written; pass --apply)"
    s = plan.summary()
    lines = [f"reconcile: {verb}", f"tenants in the catalog: {tenants}"]
    for title, key in (("tuples the platform data implies", "desired"), ("missing in OpenFGA (to add)", "to_add"), ("of objects no longer in the catalog", "orphans")):
        counts = s[key]
        total = sum(counts.values())
        lines.append(f"{title}: {total}")
        lines += [f"  {name}: {counts[name]}" for name in sorted(counts)]
    if plan.orphans:
        lines.append(f"orphan tuples are {'deleted (--prune)' if applied and prune else 'kept: pass --prune --apply to delete them'}")
    if plan.skipped_creators:
        lines.append(f"rows with a legacy creator (no owner tuple): {plan.skipped_creators}")
    if plan.skipped_email_creators:
        lines.append(f"rows with an e-mail creator (no owner tuple: cfaccess is not configured): {plan.skipped_email_creators}")
    if plan.skipped_entries:
        lines.append(f"rows skipped (an id OpenFGA cannot hold): {plan.skipped_entries}")
    return "\n".join(lines) + "\n"


async def resolve_owner_emails(emails: Sequence[str], tenant_id: str, env: Mapping[str, str]) -> list[str]:
    """For each e-mail, the account's subjects: the membership source's id (`zitadel:<id>`) and, only while `cfaccess`
    is configured, the e-mail itself (docs/iam.md, "Subjects in recorded data")."""
    if not emails:
        return []
    name = (env.get("EAP_IAM_MEMBERSHIP") or "").strip()
    factories = installed(GROUP_MEMBERSHIP, env)
    if name not in factories:
        raise SystemExit("--owner-email needs EAP_IAM_MEMBERSHIP to name an installed membership source")
    source: MembershipSource = factories[name](PluginContext(env=env))
    members = await source.list_members(tenant_id)
    subjects: list[str] = []
    for email in emails:
        found = [m for m in members if (m.email or "").lower() == email.lower()]
        if not found:
            raise SystemExit("--owner-email matches no member of the tenant")
        subjects.append(f"{source.name}:{found[0].id}")
        if email_subjects_allowed(env):
            subjects.append(email.lower())
    return subjects


def _args(argv: Sequence[str] | None, *, backfill: bool) -> argparse.Namespace:
    what = "Backfill the tuples tenant `studio` needs (links and owners)" if backfill else "Repair the resource links in OpenFGA"
    p = argparse.ArgumentParser(prog=f"python -m eap_iam_openfga.{'backfill' if backfill else 'reconcile'}", description=what)
    p.add_argument("--tenant", default=None, help="only this tenant (default: every tenant in the catalog)")
    p.add_argument("--apply", action="store_true", help="write the difference (default: a dry run that only prints it)")
    p.add_argument("--prune", action="store_true", help="with --apply, also delete the tuples of objects the catalog no longer lists")
    p.add_argument("--owner", action="append", default=[], help="a subject that becomes owner of every resource (repeatable)")
    p.add_argument("--owner-email", action="append", default=[], help="like --owner, resolved through the membership source to the account's subject (and, with cfaccess, the e-mail)")
    return p.parse_args(argv)


def default_catalog() -> ResourceCatalog:
    """The platform's catalog over `EAP_DATABASE_URL` (read only)."""
    from eap_run_store.iam_catalog import RunStoreCatalog
    from sqlalchemy import create_engine

    url = os.environ.get("EAP_DATABASE_URL", "")
    if not url:
        raise SystemExit("EAP_DATABASE_URL is not set")
    return RunStoreCatalog(create_engine(url))


async def amain(
    argv: Sequence[str] | None = None, *, backfill: bool = False, env: Mapping[str, str] | None = None, out: TextIO | None = None,
    catalog: ResourceCatalog | None = None, http: httpx2.AsyncClient | None = None,
) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    args = _args(argv, backfill=backfill)
    if args.prune and not args.apply:
        print("--prune only means something with --apply", file=sys.stderr)
        return 2
    try:
        settings = load_settings(env)
    except Exception as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    client = OpenFgaClient(settings, http)
    try:
        legacy_emails = email_subjects_allowed(env)
        owners = list(args.owner)
        if not legacy_emails and any(is_email_subject(o) for o in owners):
            print(
                "reconcile failed: an --owner is an e-mail address, and no identity provider in EAP_IAM_IDENTITY names a "
                "person by e-mail; pass the account's subject (zitadel:<id>) or --owner-email",
                file=sys.stderr,
            )
            return 2
        catalog = catalog or default_catalog()
        if args.owner_email:
            owners += await resolve_owner_emails(args.owner_email, args.tenant or env.get("EAP_BACKFILL_TENANT", "studio"), env)
        plan = await build_plan(
            client, catalog, tenant_id=args.tenant, owners=owners, creators=backfill, email_subjects=legacy_emails
        )
        if args.apply:
            await apply_plan(client, plan, prune=args.prune)
        tenants = len({t.object.split(":", 1)[1].split("/", 1)[0] for t in plan.desired})
        out.write(render(plan, applied=args.apply, prune=args.prune, tenants=tenants))
        return 0
    except PolicyUnavailable as exc:
        print(f"reconcile failed: {exc.detail}", file=sys.stderr)
        return 1
    except GrantError as exc:  # an --owner OpenFGA cannot hold: nothing was written
        print(f"reconcile failed: {exc.detail}", file=sys.stderr)
        return 2
    finally:
        if http is None:
            await client.aclose()


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
