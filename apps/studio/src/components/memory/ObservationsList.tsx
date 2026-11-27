"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  blockedByExpired,
  deleteAllObservations,
  deleteObservation,
  listObservations,
  newestObservation,
  turnRange,
  type Observation,
  type ObservationList,
  errorStatus,
} from "@/lib/memory";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toaster";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

export interface ObservationsListProps {
  /** The caller may delete notes (`run` on the conversation's agent). */
  mayDelete?: boolean;
  conversationId: string;
  /** Bump to reload (an observation or reflection was written). */
  refreshKey?: number;
}

type Pending = { kind: "one"; item: Observation } | { kind: "all" };

const EXPIRED_EXPLAINED =
  "Expired: its text aged out under the retention policy. Its turns stay covered, so the model does not see them again verbatim.";

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/**
 * The conversation's observations and reflections (active ones, oldest first) with the turns they
 * cover. Only the newest row can be deleted, an expired retention marker included: deleting an
 * older one would hide its turns from the model. Rows before a newer expired marker say so. "Delete
 * all" returns the whole history to the model verbatim (within the history limits).
 */
export function ObservationsList({ conversationId, refreshKey = 0, mayDelete = true }: ObservationsListProps) {
  const [list, setList] = useState<ObservationList | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const loaded = await listObservations(conversationId);
      if (request === requestRef.current) setList(loaded);
    } catch (reason) {
      if (request === requestRef.current) setError(messageOf(reason, "Failed to load observations."));
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    setList(null);
    void load();
  }, [load]);

  const firstRefresh = useRef(true);
  useEffect(() => {
    if (firstRefresh.current) {
      firstRefresh.current = false;
      return;
    }
    void load();
  }, [refreshKey, load]);

  const items = list?.items.filter((item) => !item.superseded_by) ?? [];
  const sorted = [...items].sort((a, b) => a.covers_to_turn - b.covers_to_turn);
  const newest = newestObservation(items);

  const confirm = async () => {
    if (!pending) return;
    setBusy(true);
    setActionError(null);
    try {
      if (pending.kind === "one") {
        await deleteObservation(conversationId, pending.item.id);
        toast.success(pending.item.expired ? "Expired observation deleted" : "Observation deleted");
      } else {
        const deleted = await deleteAllObservations(conversationId);
        toast.success(deleted === 1 ? "1 observation deleted" : `${deleted} observations deleted`);
      }
      setPending(null);
      void load();
    } catch (reason) {
      // A 409: a newer row appeared (the observer ran meanwhile); the detail names it. A 404: gone.
      setActionError(messageOf(reason, "Failed to delete."));
      if (errorStatus(reason) === 409 || errorStatus(reason) === 404) void load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section data-testid="memory-observations" aria-label="Observations" className="min-w-0 space-y-2">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <h3 className="text-label font-semibold text-foreground">
          Observations
          {list ? <span className="ml-1 font-normal text-muted-foreground">({sorted.length})</span> : null}
        </h3>
        {mayDelete && sorted.length > 0 ? (
          <Button size="sm" variant="ghost" onClick={() => setPending({ kind: "all" })} data-testid="memory-observations-delete-all">
            Delete all
          </Button>
        ) : null}
      </div>
      {list && list.covered_to_turn > 0 ? (
        <p className="text-caption text-muted-foreground" data-testid="memory-observations-covered">
          Turns 1–{list.covered_to_turn} reach the model as these notes; later turns are sent verbatim.
        </p>
      ) : null}

      {error ? (
        <div className="space-y-1">
          <p role="alert" data-testid="memory-observations-error" className="text-label text-destructive-text">
            {error}
          </p>
          <Button size="sm" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      ) : loading && !list ? (
        <p role="status" className="text-label text-muted-foreground">
          Loading observations…
        </p>
      ) : sorted.length === 0 ? (
        <p data-testid="memory-observations-empty" className="text-label text-muted-foreground">
          No observations yet. They are written after a turn once the conversation is long enough.
        </p>
      ) : (
        <ol className="min-w-0 space-y-1.5">
          {sorted.map((item) => {
            const isNewest = newest?.id === item.id;
            const blocked = blockedByExpired(items, item);
            return (
              <li
                key={item.id}
                data-testid="memory-observation-row"
                data-expired={item.expired ? "true" : "false"}
                className="min-w-0 rounded-md border border-border p-2"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <span className="text-caption font-medium text-foreground">{turnRange(item)}</span>
                  {item.kind === "reflection" ? <Badge variant="info">Reflection</Badge> : null}
                  {item.expired ? (
                    <Badge variant="warning" data-testid="memory-observation-expired">
                      Expired
                    </Badge>
                  ) : (
                    <span className="text-caption text-muted-foreground">{item.approx_tokens.toLocaleString("en-US")} tokens</span>
                  )}
                  {mayDelete && isNewest ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-auto"
                      aria-label={`Delete ${item.expired ? "expired observation" : "observation"} (${turnRange(item).toLowerCase()})`}
                      onClick={() => setPending({ kind: "one", item })}
                      data-testid="memory-observation-delete"
                    >
                      Delete
                    </Button>
                  ) : null}
                </div>
                {item.expired ? (
                  <p className="mt-1 text-caption text-muted-foreground">{EXPIRED_EXPLAINED}</p>
                ) : (
                  <p className="mt-1 whitespace-pre-wrap break-words text-caption text-foreground">{item.content}</p>
                )}
                {blocked ? (
                  <p data-testid="memory-observation-blocked" className="mt-1 text-caption text-muted-foreground">
                    A newer expired observation covers later turns: delete it first (or delete all) before this one.
                  </p>
                ) : null}
              </li>
            );
          })}
        </ol>
      )}
      {sorted.length > 1 ? (
        <p className="text-caption text-muted-foreground">Only the newest can be deleted; deleting an older one would hide its turns from the model.</p>
      ) : null}

      {pending ? (
        <ConfirmDialog
          title={pending.kind === "all" ? "Delete all observations?" : pending.item.expired ? "Delete the expired observation?" : "Delete this observation?"}
          description={
            pending.kind === "all"
              ? "Every observation and reflection of this conversation is deleted. Its turns go back to the model verbatim (within the history limits) and are observed again later."
              : `The turns it covers (${turnRange(pending.item).toLowerCase()}) go back to the model verbatim and are observed again later.`
          }
          confirmLabel={pending.kind === "all" ? "Delete all" : "Delete"}
          destructive
          busy={busy}
          error={actionError}
          onConfirm={() => void confirm()}
          onCancel={() => {
            setPending(null);
            setActionError(null);
          }}
        />
      ) : null}
    </section>
  );
}
