"use client";

import React, { useState } from "react";
import { ApiError } from "@/lib/api";
import type { AgentVersion as ApiAgentVersion } from "@/lib/api";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { VersionCompare } from "@/components/agents/VersionCompare";
import { isDirtyDraftConflict, restoreVersion, type CompareRef } from "@/lib/agentVersions";

interface AgentVersion {
  agent_id: string;
  version: number;
  spec_hash: string;
  created_at: string;
  restored_from?: number | null;
}

interface AgentVersionsPanelProps {
  versions: AgentVersion[];
  /** Enables compare and restore. Without it the panel is a read-only table. */
  agentId?: string;
  /** The editor has edits that are not saved; a restore replaces them. */
  hasUnsavedEdits?: boolean;
  /** Called with the new version after a successful restore. */
  onRestored?: (version: ApiAgentVersion) => void;
}

type RestoreState = { version: number; discard: boolean; busy: boolean; error: string | null };

export function AgentVersionsPanel({ versions, agentId, hasUnsavedEdits = false, onRestored }: AgentVersionsPanelProps) {
  const [selected, setSelected] = useState<number[]>([]);
  const [compare, setCompare] = useState<{ base: CompareRef; target: CompareRef } | null>(null);
  const [restore, setRestore] = useState<RestoreState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (versions.length === 0) {
    return null;
  }

  const interactive = agentId !== undefined;
  const latest = versions.reduce((max, v) => Math.max(max, v.version), 0);

  const toggle = (version: number) => {
    setSelected((current) =>
      current.includes(version) ? current.filter((v) => v !== version) : current.length >= 2 ? current : [...current, version],
    );
  };

  const openSelectedCompare = () => {
    if (selected.length !== 2) return;
    const [a, b] = [...selected].sort((x, y) => x - y);
    setCompare({ base: a, target: b });
  };

  const runRestore = async (state: RestoreState) => {
    if (!agentId) return;
    setRestore({ ...state, busy: true, error: null });
    try {
      const created = await restoreVersion(agentId, state.version, { discardDraft: state.discard });
      setRestore(null);
      setSelected([]);
      setNotice(`Version ${created.version} published, restored from v${state.version}`);
      onRestored?.(created);
    } catch (err: unknown) {
      if (err instanceof ApiError && !state.discard && isDirtyDraftConflict(err.status, err.message)) {
        setRestore({ version: state.version, discard: true, busy: false, error: null });
        return;
      }
      const message = err instanceof ApiError && err.message ? err.message : "Failed to restore version";
      setRestore({ ...state, busy: false, error: message });
    }
  };

  const restoreDialog = restore ? (
    restore.discard ? (
      <ConfirmDialog
        title={`Discard draft and restore version ${restore.version}?`}
        description={
          <>
            <p>The draft has unpublished changes since version {latest}.</p>
            <p className="mt-2">
              Restoring publishes version {latest + 1} with the spec of version {restore.version} and replaces the draft. The unpublished
              draft changes are lost.
            </p>
          </>
        }
        confirmLabel="Discard draft and restore"
        destructive
        busy={restore.busy}
        error={restore.error}
        onConfirm={() => runRestore(restore)}
        onCancel={() => setRestore(null)}
      />
    ) : (
      <ConfirmDialog
        title={`Restore version ${restore.version}?`}
        description={
          <>
            <p>
              This publishes a new version, {latest + 1}, with the spec of version {restore.version}. Existing versions are not changed,
              and the draft becomes the restored spec.
            </p>
            {hasUnsavedEdits ? <p className="mt-2">Your unsaved edits in the editor will be lost.</p> : null}
          </>
        }
        confirmLabel="Restore and publish"
        busy={restore.busy}
        error={restore.error}
        onConfirm={() => runRestore(restore)}
        onCancel={() => setRestore(null)}
      />
    )
  ) : null;

  return (
    <div className="border-t pt-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold text-card-foreground">Versions</h2>
        {interactive ? (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setCompare({ base: latest, target: "draft" })}
              className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring"
            >
              Compare draft with latest
            </button>
            <button
              type="button"
              onClick={openSelectedCompare}
              disabled={selected.length !== 2}
              aria-describedby="compare-selected-hint"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
            >
              Compare selected
            </button>
          </div>
        ) : null}
      </div>
      {interactive ? (
        <p id="compare-selected-hint" className="mb-2 text-xs text-muted-foreground">
          Select two versions to compare them ({selected.length} of 2 selected).
        </p>
      ) : null}
      {notice ? (
        <p role="status" className="mb-3 rounded-md border border-border bg-muted p-3 text-sm text-foreground">{notice}</p>
      ) : null}
      {compare && agentId ? (
        <div className="mb-4">
          <VersionCompare agentId={agentId} base={compare.base} target={compare.target} onClose={() => setCompare(null)} />
        </div>
      ) : null}
      <div className="bg-muted rounded-md overflow-hidden">
        <table className="min-w-full divide-y divide-border">
          <thead className="bg-muted">
            <tr>
              {interactive ? (
                <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">
                  <span className="sr-only">Select for compare</span>
                </th>
              ) : null}
              <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Version</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Hash</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">Created</th>
              {interactive ? (
                <th className="px-4 py-2 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider">
                  <span className="sr-only">Actions</span>
                </th>
              ) : null}
            </tr>
          </thead>
          <tbody className="bg-card divide-y divide-border">
            {versions.map((version) => {
              const isSelected = selected.includes(version.version);
              return (
                <tr key={`${version.agent_id}-${version.version}`}>
                  {interactive ? (
                    <td className="px-4 py-2">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        disabled={!isSelected && selected.length >= 2}
                        onChange={() => toggle(version.version)}
                        aria-label={`Select version ${version.version} for compare`}
                        className="h-4 w-4 accent-primary"
                      />
                    </td>
                  ) : null}
                  <td className="px-4 py-2 whitespace-nowrap text-sm font-medium text-card-foreground">
                    {version.version}
                    {version.version === latest ? <span className="ml-2 text-xs font-normal text-muted-foreground">latest</span> : null}
                    {version.restored_from != null ? (
                      <span className="ml-2 text-xs font-normal text-muted-foreground">restored from v{version.restored_from}</span>
                    ) : null}
                  </td>
                  <td className="px-4 py-2 whitespace-nowrap text-sm text-muted-foreground font-mono">
                    {version.spec_hash.slice(0, 8)}
                  </td>
                  <td className="px-4 py-2 whitespace-nowrap text-sm text-muted-foreground">
                    {new Date(version.created_at).toLocaleString()}
                  </td>
                  {interactive ? (
                    <td className="px-4 py-2 whitespace-nowrap text-right text-sm">
                      {version.version !== latest ? (
                        <button
                          type="button"
                          onClick={() => {
                            setNotice(null);
                            setRestore({ version: version.version, discard: false, busy: false, error: null });
                          }}
                          aria-label={`Restore version ${version.version}`}
                          className="rounded-md border border-border px-2 py-1 text-foreground hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring"
                        >
                          Restore this version
                        </button>
                      ) : null}
                    </td>
                  ) : null}
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="px-4 py-2 bg-muted text-sm text-muted-foreground">
          Showing {versions.length} version{versions.length !== 1 ? "s" : ""}
        </div>
      </div>
      {restoreDialog}
    </div>
  );
}
