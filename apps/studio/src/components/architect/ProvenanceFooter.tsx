"use client";

import React, { useEffect, useState } from "react";
import { changedNotice, footerLine, getProvenance, shortHash, type Provenance, type ProvenanceKind } from "@/lib/provenance";

export interface ProvenanceFooterProps {
  kind: ProvenanceKind;
  /** The definition's slug or UUID. */
  definitionId: string;
  /** Which card this footer sits on: the draft card shows the latest draft row, the publish card the publish row. */
  card: "draft" | "publish";
  /** Test seam: the loader. */
  load?: typeof getProvenance;
}

/**
 * "Based on" footer of a draft or publish card: platform version and capability snapshot on one line,
 * expandable to the manifest, the tool calls the Architect made (names only) and the capabilities that changed
 * since the draft was made. Renders nothing when there is no provenance (a definition not made by the Architect,
 * or one saved before provenance existed) or the read fails: it never blocks the card.
 */
export function ProvenanceFooter({ kind, definitionId, card, load = getProvenance }: ProvenanceFooterProps) {
  const [data, setData] = useState<Provenance | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let live = true;
    load(kind, definitionId, card)
      .then((result) => live && setData(result))
      .catch(() => live && setData(null));
    return () => {
      live = false;
    };
  }, [kind, definitionId, card, load]);
  const row = data?.items[0];
  if (!data || !row) return null;
  const notice = card === "draft" ? changedNotice(data.capabilities_changed) : null;
  return (
    <div data-testid={card === "draft" ? "provenance-footer-draft" : "provenance-footer-publish"} className="mt-2 border-t border-border pt-2 text-caption text-muted-foreground">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="text-start hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span data-testid="provenance-line">Based on: {footerLine(row)}</span>
      </button>
      {notice ? (
        <p data-testid="provenance-changed" className="mt-1 text-warning">
          {notice}: {data.capabilities_changed.map((c) => `${c.id} (${c.was ?? "absent"} → ${c.now ?? "absent"})`).join(", ")}
        </p>
      ) : null}
      {open ? (
        <dl data-testid="provenance-details" className="mt-1 space-y-0.5">
          <div>Manifest version {row.manifest_version ?? "unknown"} · hash {shortHash(row.manifest_hash, 12)}</div>
          <div>Capability registry {shortHash(row.registry_version, 12)} · snapshot {shortHash(row.capability_snapshot_hash, 12)}</div>
          {row.model ? <div>Model {row.model}</div> : null}
          {row.degraded.length > 0 ? <div>Missing layers: {row.degraded.join(", ")}</div> : null}
          <div>
            Tool calls: {row.tool_calls.length === 0 ? "none recorded" : row.tool_calls.map((call) => call.name).join(", ")}
          </div>
        </dl>
      ) : null}
    </div>
  );
}
