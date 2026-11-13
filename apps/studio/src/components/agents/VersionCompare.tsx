"use client";

import React, { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import {
  compareRefLabel,
  compareVersions,
  type CompareRef,
  type InstructionsDiffLine,
  type SpecChange,
  type VersionCompareResult,
} from "@/lib/agentVersions";

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

const KIND_LABEL: Record<SpecChange["kind"], string> = { added: "Added", removed: "Removed", changed: "Changed" };

interface DiffRow {
  base: InstructionsDiffLine | null;
  target: InstructionsDiffLine | null;
}

/** Pairs each run of removed lines with the following run of added lines, so edits sit side by side. */
export function sideBySideRows(lines: InstructionsDiffLine[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.op === "equal") {
      rows.push({ base: line, target: line });
      i += 1;
      continue;
    }
    const removed: InstructionsDiffLine[] = [];
    const added: InstructionsDiffLine[] = [];
    while (i < lines.length && lines[i].op === "removed") removed.push(lines[i++]);
    while (i < lines.length && lines[i].op === "added") added.push(lines[i++]);
    for (let k = 0; k < Math.max(removed.length, added.length); k += 1) {
      rows.push({ base: removed[k] ?? null, target: added[k] ?? null });
    }
  }
  return rows;
}

function DiffCell({ line, side }: { line: InstructionsDiffLine | null; side: "base" | "target" }) {
  if (!line) return <td className="w-1/2 bg-muted px-2 py-0.5" aria-label="no line" />;
  const number = side === "base" ? line.base_line : line.target_line;
  const changed = line.op !== "equal";
  const tone = !changed ? "" : line.op === "removed" ? "bg-destructive/10 text-foreground" : "bg-primary/10 text-foreground";
  const marker = !changed ? " " : line.op === "removed" ? "−" : "+";
  return (
    <td className={`w-1/2 px-2 py-0.5 align-top ${tone}`} data-op={line.op}>
      <span className="mr-2 inline-block w-8 select-none text-right text-muted-foreground">{number ?? ""}</span>
      <span aria-hidden="true" className="mr-1 select-none text-muted-foreground">{marker}</span>
      {changed ? <span className="sr-only">{line.op === "removed" ? "removed: " : "added: "}</span> : null}
      <span className="whitespace-pre-wrap break-words">{line.text}</span>
    </td>
  );
}

/** Renders a compare result: changed fields and a side-by-side instructions diff. */
export function VersionCompareView({ result }: { result: VersionCompareResult }) {
  const baseLabel = compareRefLabel(result.base.version);
  const targetLabel = compareRefLabel(result.target.version);
  const instructionsChanged = result.instructions_diff.some((line) => line.op !== "equal");
  const fieldChanges = result.changes.filter((change) => change.path !== "instructions");

  if (result.identical) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        {baseLabel} and {targetLabel} have the same spec.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <section aria-labelledby="compare-fields-heading">
        <h3 id="compare-fields-heading" className="mb-2 text-sm font-semibold text-card-foreground">Changed fields</h3>
        {fieldChanges.length === 0 ? (
          <p className="text-sm text-muted-foreground">Only the instructions changed.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="min-w-full divide-y divide-border text-sm">
              <caption className="sr-only">Changed fields from {baseLabel} to {targetLabel}</caption>
              <thead className="bg-muted">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wider text-muted-foreground">Field</th>
                  <th scope="col" className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wider text-muted-foreground">Change</th>
                  <th scope="col" className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wider text-muted-foreground">{baseLabel}</th>
                  <th scope="col" className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wider text-muted-foreground">{targetLabel}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border bg-card">
                {fieldChanges.map((change) => (
                  <tr key={change.path} data-testid="compare-change">
                    <th scope="row" className="px-3 py-2 text-left align-top font-mono text-xs font-normal text-card-foreground">{change.path}</th>
                    <td className="px-3 py-2 align-top text-muted-foreground">{KIND_LABEL[change.kind]}</td>
                    <td className="px-3 py-2 align-top"><pre className="whitespace-pre-wrap break-words font-mono text-xs text-foreground">{formatValue(change.before)}</pre></td>
                    <td className="px-3 py-2 align-top"><pre className="whitespace-pre-wrap break-words font-mono text-xs text-foreground">{formatValue(change.after)}</pre></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {result.truncated ? (
          <p className="mt-2 text-xs text-muted-foreground">The change list was cut short; not every change is shown.</p>
        ) : null}
      </section>

      <section aria-labelledby="compare-instructions-heading">
        <h3 id="compare-instructions-heading" className="mb-2 text-sm font-semibold text-card-foreground">Instructions</h3>
        {!instructionsChanged ? (
          <p className="text-sm text-muted-foreground">No changes to the instructions.</p>
        ) : (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full table-fixed font-mono text-xs" data-testid="instructions-diff">
              <caption className="sr-only">Instructions diff, {baseLabel} on the left, {targetLabel} on the right</caption>
              <thead className="bg-muted">
                <tr>
                  <th scope="col" className="px-2 py-1 text-left font-sans text-xs font-medium text-muted-foreground">{baseLabel}</th>
                  <th scope="col" className="px-2 py-1 text-left font-sans text-xs font-medium text-muted-foreground">{targetLabel}</th>
                </tr>
              </thead>
              <tbody className="bg-card text-foreground">
                {sideBySideRows(result.instructions_diff).map((row, index) => (
                  <tr key={index}>
                    <DiffCell line={row.base} side="base" />
                    <DiffCell line={row.target} side="target" />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

interface VersionCompareProps {
  agentId: string;
  base: CompareRef;
  target: CompareRef;
  onClose: () => void;
}

/** Fetches and shows the compare of `base` and `target` (a version number or "draft"). */
export function VersionCompare({ agentId, base, target, onClose }: VersionCompareProps) {
  const [result, setResult] = useState<VersionCompareResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setResult(null);
    setError(null);
    compareVersions(agentId, base, target)
      .then((data) => {
        if (!cancelled) setResult(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError && err.message ? err.message : "Failed to compare versions");
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, base, target]);

  const title = `Compare ${compareRefLabel(base)} with ${compareRefLabel(target)}`;
  return (
    <section aria-label={title} data-testid="version-compare" className="space-y-4 rounded-md border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-card-foreground">{title}</h2>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-border px-3 py-1 text-sm text-foreground hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring"
        >
          Close compare
        </button>
      </div>
      {error ? (
        <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</p>
      ) : result ? (
        <VersionCompareView result={result} />
      ) : (
        <p role="status" className="text-sm text-muted-foreground">Loading compare…</p>
      )}
    </section>
  );
}
