"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import {
  APPROVAL_FOLLOW_MAX_DELAY_MS,
  APPROVAL_FOLLOW_MAX_FAILURES,
  APPROVAL_FOLLOW_POLL_MS,
  APPROVAL_KIND_LABELS,
  APPROVAL_KINDS,
  applyDecision,
  approvalFiltersToParams,
  approvalId,
  approvalSubject,
  findApproval,
  hasActiveApprovalFilters,
  listApprovals,
  parseApprovalFilters,
  parseApprovalId,
  raceNotice,
  refreshPendingApprovals,
  type Approval,
  type ApprovalFilters,
  type ApprovalKind,
  type ApprovalStatus,
  TERMINAL_RUN_STATUSES,
} from "@/lib/approvals";
import { routes } from "@/lib/routes";
import { APPROVAL_SHEET_PARAM } from "@/lib/routes/approvals";
import { TRACE_PARAM, TRACE_TAB_PARAM } from "@/lib/routes/observability";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { Button } from "@/components/ui/Button";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { useUrlSheet } from "@/components/ui/useUrlSheet";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { ApprovalSheet } from "./ApprovalSheet";
import { ApprovalStatusBadge } from "./ApprovalStatusBadge";

const inputClass =
  "rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
const labelClass = "text-caption font-medium text-muted-foreground";

function formatWhen(iso: string | null): string {
  if (!iso) return "-";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function errorText(reason: unknown): string {
  return reason instanceof Error && reason.message ? reason.message : "The approvals could not be loaded.";
}

interface Lookup {
  id: string;
  approval: Approval | null;
  /** The lookup failed (not a 404): the Sheet says so instead of "not found". */
  error: string | null;
}

/**
 * The Approvals inbox (AC.15): every human decision of the tenant, pending (newest request first)
 * or decided (newest decision first), filtered by kind and agent in the URL. A row opens the
 * approval Sheet (`?approval=<run_id>:<key>`), where a pending item is decided inline; the row
 * then shows its decision and the sidebar count follows.
 */
export function ApprovalsInbox() {
  const router = useRouter();
  const pathname = usePathname() ?? routes.approvals();
  const searchParams = useSearchParams();
  const filtersKey = searchParams?.toString() ?? "";
  const filters = useMemo(() => parseApprovalFilters(new URLSearchParams(filtersKey)), [filtersKey]);
  const sheet = useUrlSheet(APPROVAL_SHEET_PARAM, { history: "push" });

  const [rows, setRows] = useState<Approval[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});
  const [lookup, setLookup] = useState<Lookup | null>(null);
  // After a lost race (409): the explanation for the open Sheet, keyed by its approval id.
  const [notice, setNotice] = useState<{ id: string; text: string } | null>(null);
  const request = useRef(0);

  const filterKey = `${filters.status}|${filters.kind ?? ""}|${filters.agent_id ?? ""}`;

  const load = useCallback(async (current: ApprovalFilters) => {
    const mine = ++request.current;
    setLoading(true);
    setError(null);
    try {
      const page = await listApprovals(current);
      if (mine !== request.current) return;
      setRows(page.items);
      setNextCursor(page.next_cursor);
    } catch (reason) {
      if (mine !== request.current) return;
      setError(errorText(reason));
    } finally {
      if (mine === request.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(filters);
    // filterKey stands for the filters' content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, load]);

  useEffect(() => {
    let live = true;
    api.agents
      .list()
      .then((agents) => {
        if (live) setAgentNames(Object.fromEntries(agents.map((agent) => [agent.id, agent.name || agent.id])));
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  const loadMore = useCallback(async () => {
    if (!nextCursor) return;
    const mine = request.current;
    setLoadingMore(true);
    try {
      const page = await listApprovals(filters, { cursor: nextCursor });
      if (mine !== request.current) return;
      setRows((previous) => {
        const seen = new Set(previous.map(approvalId));
        return [...previous, ...page.items.filter((item) => !seen.has(approvalId(item)))];
      });
      setNextCursor(page.next_cursor);
    } catch (reason) {
      if (mine === request.current) setError(errorText(reason));
    } finally {
      setLoadingMore(false);
    }
  }, [filters, nextCursor]);

  const setFilters = useCallback(
    (change: Partial<ApprovalFilters>) => {
      const params = approvalFiltersToParams({ ...filters, ...change });
      // Keep an open Sheet (approval, and a trace stacked on it) across filter changes.
      for (const key of [APPROVAL_SHEET_PARAM, TRACE_PARAM, TRACE_TAB_PARAM]) {
        const value = searchParams?.get(key);
        if (value) params.set(key, value);
      }
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [filters, pathname, router, searchParams],
  );

  // The open approval: a loaded row, else looked up (a shared link, or a row on a later page).
  const selectedId = sheet.value;
  const inRows = selectedId ? rows.find((row) => approvalId(row) === selectedId) ?? null : null;
  useEffect(() => {
    if (!selectedId || inRows || loading) return;
    if (lookup?.id === selectedId) return;
    const parsed = parseApprovalId(selectedId);
    if (!parsed) {
      setLookup({ id: selectedId, approval: null, error: null });
      return;
    }
    let live = true;
    findApproval(parsed.runId, parsed.key)
      .then((found) => {
        if (live) setLookup({ id: selectedId, approval: found, error: null });
      })
      .catch((reason: unknown) => {
        if (live) setLookup({ id: selectedId, approval: null, error: reason instanceof Error && reason.message ? reason.message : "The approval could not be loaded." });
      });
    return () => {
      live = false;
    };
  }, [inRows, loading, lookup?.id, selectedId]);
  const looked = lookup && lookup.id === selectedId ? lookup : null;
  const selected = inRows ?? looked?.approval ?? null;
  const missing = Boolean(selectedId && !selected && looked && looked.approval === null);
  const lookupError = missing ? looked?.error ?? null : null;

  // A notice belongs to one Sheet: closing it or moving to another approval drops it.
  useEffect(() => {
    setNotice((previous) => (previous && previous.id === selectedId ? previous : null));
  }, [selectedId]);

  /** Put a re-fetched item in place: its row (if listed) and the Sheet's own copy. */
  const putFresh = useCallback((id: string, fresh: Approval) => {
    setRows((previous) => previous.map((row) => (approvalId(row) === id ? fresh : row)));
    setLookup({ id, approval: fresh, error: null });
  }, []);

  const onDecided = useCallback(
    (approval: Approval, decision: "approved" | "denied") => {
      const id = approvalId(approval);
      setRows((previous) => previous.map((row) => (approvalId(row) === id ? applyDecision(row, decision) : row)));
      setLookup((previous) => (previous && previous.approval && previous.id === id ? { ...previous, approval: applyDecision(previous.approval, decision) } : previous));
      void refreshPendingApprovals();
      // The card hands over only the decision: re-read the item for who decided and when.
      findApproval(approval.run_id, approval.approval_key, { kind: approval.kind })
        .then((fresh) => {
          if (fresh && fresh.decision !== null) putFresh(id, fresh);
        })
        .catch(() => undefined);
    },
    [putFresh],
  );

  /**
   * Follow a decided item while its Sheet is open and its run has not ended: the decision resumes
   * the run, so the Sheet (and its row) re-read the item every APPROVAL_FOLLOW_POLL_MS until the
   * run status is terminal, as the trace Sheet follows a running run. A copy without a decision
   * (the projection not caught up yet) is not put in place, so the decision controls never return.
   * An item without a run status (its run unknown) is not followed. It stops, with a notice, when
   * the item is no longer found, or after APPROVAL_FOLLOW_MAX_FAILURES failed re-reads in a row
   * (each waiting twice as long as the last, up to APPROVAL_FOLLOW_MAX_DELAY_MS). One request at a
   * time: the next is scheduled only once the last has settled.
   */
  const followed =
    selected && selected.status === "decided" && selected.run_status != null && !TERMINAL_RUN_STATUSES.has(selected.run_status) ? selected : null;
  const followedRef = useRef<Approval | null>(followed);
  useEffect(() => {
    followedRef.current = followed;
  });
  const followId = followed ? approvalId(followed) : null;
  useEffect(() => {
    if (!followId) return;
    const id = followId;
    let live = true;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const schedule = (ms: number) => {
      timer = setTimeout(tick, ms);
    };
    function tick() {
      const current = followedRef.current;
      if (!live || !current || approvalId(current) !== id) return;
      findApproval(current.run_id, current.approval_key, { kind: current.kind })
        .then((fresh) => {
          if (!live) return;
          failures = 0;
          if (fresh === null) {
            setNotice({ id: id, text: "This approval is no longer found, so the Sheet stopped following its run." });
            return;
          }
          if (fresh.decision !== null) putFresh(id, fresh);
          schedule(APPROVAL_FOLLOW_POLL_MS);
        })
        .catch((reason: unknown) => {
          if (!live) return;
          failures += 1;
          if (failures >= APPROVAL_FOLLOW_MAX_FAILURES) {
            const detail = reason instanceof Error && reason.message ? reason.message : "the approval could not be loaded";
            setNotice({ id: id, text: `Stopped following the run: ${detail}. Reopen the Sheet to try again.` });
            return;
          }
          schedule(Math.min(APPROVAL_FOLLOW_POLL_MS * 2 ** failures, APPROVAL_FOLLOW_MAX_DELAY_MS));
        });
    }
    schedule(APPROVAL_FOLLOW_POLL_MS);
    return () => {
      live = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [followId, putFresh]);

  /**
   * The card lost a race (409). The Sheet stays on the same item: its fresh state (decided, timed
   * out, or its run ended) is put in place before the list reloads, so the Sheet never loses its
   * item when the reloaded Pending list no longer holds it; the notice says what happened.
   */
  const onConflict = useCallback(
    async (approval: Approval) => {
      const id = approvalId(approval);
      let fresh: Approval | null = null;
      try {
        fresh = await findApproval(approval.run_id, approval.approval_key, { kind: approval.kind });
      } catch {
        fresh = null;
      }
      // Without a fresh copy, keep the old one, marked as no longer pending so the card does not
      // come back (the API said it no longer waits); its badge reads "No longer waiting".
      const shown = fresh ?? { ...approval, status: "decided" as const };
      putFresh(id, shown);
      setNotice({ id, text: raceNotice(fresh) });
      await Promise.all([load(filters), refreshPendingApprovals()]);
    },
    [filters, load, putFresh],
  );

  const agentOptions = Object.entries(agentNames).sort((a, b) => a[1].localeCompare(b[1]));
  const sourceOf = (row: Approval): string => {
    if (row.kind === "step") return row.workflow_id ? `Workflow ${row.workflow_id.slice(0, 8)}` : "Workflow";
    const agent = row.agent_id ? agentNames[row.agent_id] ?? row.agent_id.slice(0, 8) : "Agent";
    return row.root_run_id !== row.run_id ? `${agent} (in a workflow)` : agent;
  };

  const decided = filters.status === "decided";
  const columns: DataListColumn<Approval>[] = [
    {
      id: "request",
      header: "Request",
      cell: (row) => <span className="font-mono">{approvalSubject(row)}</span>,
      sortable: true,
      sortValue: approvalSubject,
      className: "min-w-0 max-w-64 truncate",
    },
    { id: "kind", header: "Kind", cell: (row) => APPROVAL_KIND_LABELS[row.kind], sortable: true, className: "hidden sm:table-cell", headerClassName: "hidden sm:table-cell" },
    { id: "source", header: "Agent or workflow", cell: sourceOf, sortable: true, sortValue: sourceOf, className: "hidden md:table-cell min-w-0 max-w-48 truncate", headerClassName: "hidden md:table-cell" },
    {
      id: "requested",
      header: "Requested",
      cell: (row) => <time dateTime={row.requested_at ?? undefined}>{formatWhen(row.requested_at)}</time>,
      sortable: true,
      sortValue: (row) => row.requested_at ?? "",
      className: "hidden lg:table-cell whitespace-nowrap",
      headerClassName: "hidden lg:table-cell",
    },
    decided
      ? {
          id: "decided",
          header: "Decided",
          cell: (row) => <time dateTime={row.decided_at ?? undefined}>{formatWhen(row.decided_at)}</time>,
          sortable: true,
          sortValue: (row) => row.decided_at ?? "",
          className: "whitespace-nowrap",
        }
      : {
          id: "deadline",
          header: "Deadline",
          cell: (row) => <time dateTime={row.deadline ?? undefined}>{formatWhen(row.deadline)}</time>,
          sortable: true,
          sortValue: (row) => row.deadline ?? "",
          className: "whitespace-nowrap",
        },
    { id: "status", header: "Status", cell: (row) => <ApprovalStatusBadge approval={row} testId={`approval-row-status-${approvalId(row)}`} />, align: "end" },
  ];

  const emptyTitle = hasActiveApprovalFilters(filters)
    ? "No approvals match these filters."
    : decided
      ? "No decided approvals yet."
      : "Nothing is waiting for approval.";
  const emptyDescription = hasActiveApprovalFilters(filters)
    ? undefined
    : "Gated agent tool calls and workflow approval steps appear here while they wait for a decision.";

  return (
    <TraceSheetHost>
      <div className="flex min-w-0 flex-col gap-4 p-4">
        <p className="text-label text-muted-foreground">Tool calls and workflow steps waiting for a person, and the decisions made</p>

        <div role="search" aria-label="Approval filters" data-testid="approvals-filter-bar" className="flex min-w-0 flex-wrap items-end gap-3 rounded-lg border border-border bg-surface p-3">
          <div className="flex min-w-0 flex-col gap-1">
            <span className={labelClass} aria-hidden="true">Status</span>
            <SegmentedControl<ApprovalStatus>
              label="Status"
              value={filters.status}
              onValueChange={(status) => setFilters({ status })}
              options={[
                { value: "pending", label: "Pending" },
                { value: "decided", label: "Decided" },
              ]}
            />
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="approvals-kind-filter" className={labelClass}>Kind</label>
            <select
              id="approvals-kind-filter"
              data-testid="approvals-kind-filter"
              value={filters.kind ?? "all"}
              onChange={(event) => setFilters({ kind: event.target.value === "all" ? null : (event.target.value as ApprovalKind) })}
              className={inputClass}
            >
              <option value="all">All</option>
              {APPROVAL_KINDS.map((kind) => (
                <option key={kind} value={kind}>{APPROVAL_KIND_LABELS[kind]}</option>
              ))}
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="approvals-agent-filter" className={labelClass}>Agent</label>
            <select
              id="approvals-agent-filter"
              data-testid="approvals-agent-filter"
              value={filters.agent_id ?? ""}
              onChange={(event) => setFilters({ agent_id: event.target.value || null })}
              className={`${inputClass} max-w-48`}
            >
              <option value="">All agents</option>
              {agentOptions.map(([id, name]) => (
                <option key={id} value={id}>{name}</option>
              ))}
              {filters.agent_id && !agentNames[filters.agent_id] ? <option value={filters.agent_id}>{filters.agent_id.slice(0, 8)}...</option> : null}
            </select>
          </div>
          {hasActiveApprovalFilters(filters) ? (
            <Button variant="ghost" size="sm" onClick={() => setFilters({ kind: null, agent_id: null })} data-testid="approvals-clear-filters">
              Clear filters
            </Button>
          ) : null}
        </div>

        <DataList<Approval>
          label="Approvals"
          rows={rows}
          columns={columns}
          getRowId={approvalId}
          onRowOpen={(row) => sheet.open(approvalId(row))}
          getRowLabel={(row) => `Open the approval for ${APPROVAL_KIND_LABELS[row.kind].toLowerCase()} ${approvalSubject(row)}`}
          getRowTestId={(row) => `approval-row-${approvalId(row)}`}
          selectedRowId={selected ? approvalId(selected) : null}
          loading={loading && rows.length === 0}
          error={error}
          onRetry={() => void load(filters)}
          empty={{ title: emptyTitle, description: emptyDescription }}
          testId="approvals-list"
        />

        {nextCursor ? (
          <div className="flex justify-center">
            <Button variant="secondary" size="sm" onClick={() => void loadMore()} disabled={loadingMore} data-testid="approvals-load-more">
              {loadingMore ? "Loading..." : "Load more"}
            </Button>
          </div>
        ) : null}
      </div>

      <ApprovalSheet
        approval={selected}
        missing={missing}
        missingError={lookupError}
        agentName={selected?.agent_id ? agentNames[selected.agent_id] ?? null : null}
        onOpenChange={sheet.onOpenChange}
        onDecided={onDecided}
        onConflict={onConflict}
        notice={notice && selected && notice.id === approvalId(selected) ? notice.text : null}
        onRetryLookup={() => setLookup(null)}
      />
    </TraceSheetHost>
  );
}
