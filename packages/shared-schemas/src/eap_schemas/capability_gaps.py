"""What the platform cannot do yet, declared in code beside the registry (system-aware Architect, phase A).

A gap is one of three things:

- `designed_not_built`: a design exists and the code does not. Each declares the registry entry it will become
  (`entry_id`), created here with status `designed_not_built`, so the Architect finds "inbound webhooks" in the
  registry and can refuse against it. The PR that ships the capability flips the declaration (the status to
  `available`, the entry then comes from the real declaration module) and removes the gap.
- `architect_format`: the runtime has it, the Architect's authoring format cannot express it (`step.foreach`).
  The entries are the existing `available` ones; they carry `writable_by_architect: false`.
- `not_planned`: absent and not designed. No registry entry (there is nothing to point at).

`designed_in` is a path under the repository that exists on `main`, or `pr:<number>` for a design that lives
only on an open pull request (`pr:111`). A design branch that has not merged has no file on `main` to point at, and
pointing at a path that does not exist is exactly what the checks forbid; the pull request is the reference that
survives until the design merges, when the declaration should switch to its path. `designed_in_problem()` is the
one check both tests and CI use.

`known_gaps()` is generated from these declarations, never hand-written elsewhere.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from eap_schemas.capabilities import CapabilityEntry, CapabilityKind, architect_step_kinds, platform_step_kinds

GapStatus = Literal["designed_not_built", "not_planned", "architect_format"]
PR_REF = re.compile(r"^pr:[1-9]\d{0,5}$")


class GapDecl(BaseModel):
    """One declared gap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str  # "gap.inbound_webhook"
    title: str
    status: GapStatus
    short: str | None = None  # the entry's own title ("Inbound webhook"), for designed_not_built
    kind: CapabilityKind | None = None  # the entry's kind, for designed_not_built
    entry_id: str | None = None  # the entry a designed_not_built gap creates ("trigger.webhook")
    designed_in: str | None = None  # a docs path that exists, or `pr:<number>`
    alternatives: tuple[str, ...] = ()  # capability ids that partly cover it
    needs_note: str | None = None  # when an alternative is valid ("only when the source can be queried")


class GapLine(BaseModel):
    """A gap as the registry's read API and the Architect's tools return it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    status: GapStatus
    designed_in: str | None = None
    alternatives: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()  # the registry entries it concerns (none for not_planned)
    note: str | None = None


GAPS: tuple[GapDecl, ...] = (
    GapDecl(
        id="gap.inbound_webhook", short="Inbound webhook trigger", kind="trigger", entry_id="trigger.webhook", status="designed_not_built",
        title="Inbound webhooks (HMAC-verified) that start a run or wake a waiting step",
        designed_in="pr:111", alternatives=("trigger.schedule",),
        needs_note="polling on a schedule is valid only when the source can be queried for recent items",
    ),
    GapDecl(
        id="gap.wait_event_step", short="wait_event step", kind="step", entry_id="step.wait_event", status="designed_not_built",
        title="wait_event step: wait for an external event before continuing", designed_in="pr:111",
        alternatives=("step.approval",),
        needs_note="an approval step waits for a person, not for an event",
    ),
    GapDecl(
        id="gap.a2a_agent", short="a2a_agent step", kind="step", entry_id="step.a2a_agent", status="designed_not_built",
        title="A2A: call an external agent over the Agent2Agent protocol", designed_in="pr:100",
        alternatives=("feature.agent_tools",),
        needs_note="agents as tools call platform agents only, not external ones",
    ),
    GapDecl(
        id="gap.iam_per_user", short="Per-user identity and roles", kind="feature", entry_id="feature.iam_per_user", status="designed_not_built",
        title="Per-user identity, roles, per-resource grants and declared permissions",
        designed_in="docs/iam.md",
    ),
    GapDecl(
        id="gap.architect_foreach_loop", status="architect_format",
        title="The runtime has these step kinds, the Architect's format cannot write them: a person builds them in the editor",
    ),
    GapDecl(id="gap.run_code", status="not_planned", title="No code-execution step: a workflow cannot run code"),
    GapDecl(
        id="gap.kb_usage_analytics", status="not_planned",
        title="Knowledge base usage and answer-quality analytics are not recorded",
    ),
    GapDecl(
        id="gap.event_triggers_other", status="not_planned",
        title="No trigger fires from a knowledge base sync finishing, a run ending or an approval",
        alternatives=("trigger.schedule",),
    ),
    GapDecl(
        id="gap.push_notifications", status="not_planned",
        title="No outbound notification channel except an http.request call the author composes",
        alternatives=("tool.http.request",),
    ),
)


def architect_format_entry_ids() -> tuple[str, ...]:
    """The step entries the runtime has and the Architect's authoring format cannot write."""
    writable = set(architect_step_kinds())
    return tuple(f"step.{kind}" for kind in platform_step_kinds() if kind not in writable)


def gap_entries(gaps: tuple[GapDecl, ...] = GAPS) -> list[CapabilityEntry]:
    """The `designed_not_built` entries the gaps declare."""
    out: list[CapabilityEntry] = []
    for gap in gaps:
        if gap.status != "designed_not_built":
            continue
        assert gap.entry_id and gap.kind, f"{gap.id}: a designed_not_built gap needs kind and entry_id"
        out.append(CapabilityEntry(
            id=gap.entry_id, kind=gap.kind, title=gap.short or gap.title[:80], description=gap.title[:300],
            status="designed_not_built", status_reason="designed, not built", designed_in=gap.designed_in,
            writable_by_architect=False,
        ))
    return out


def known_gaps(gaps: tuple[GapDecl, ...] = GAPS) -> tuple[GapLine, ...]:
    """The registry's known gaps, generated from the declarations."""
    out: list[GapLine] = []
    for gap in gaps:
        entries = (gap.entry_id,) if gap.entry_id else (architect_format_entry_ids() if gap.status == "architect_format" else ())
        out.append(GapLine(
            id=gap.id, title=gap.title, status=gap.status, designed_in=gap.designed_in,
            alternatives=gap.alternatives, entry_ids=entries, note=gap.needs_note,
        ))
    return tuple(sorted(out, key=lambda g: g.id))


def designed_in_problem(value: str | None, path_exists: Callable[[str], bool]) -> str | None:
    """Why a `designed_in` is unusable, or None: it must be a path under the repo that exists, or `pr:<number>`."""
    if value is None:
        return "designed_in is missing"
    if PR_REF.fullmatch(value):
        return None
    if value.startswith(("/", "http:", "https:")) or ".." in value.split("/"):
        return f"designed_in {value!r} is neither a repository path nor pr:<number>"
    return None if path_exists(value) else f"designed_in {value!r} is not a file on main"


__all__ = [
    "GAPS",
    "GapDecl",
    "GapLine",
    "GapStatus",
    "PR_REF",
    "architect_format_entry_ids",
    "designed_in_problem",
    "gap_entries",
    "known_gaps",
]
