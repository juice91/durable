"use client";

import React, { useCallback, useRef, useState } from "react";
import Link from "next/link";
import { MoreHorizontal } from "lucide-react";
import { ApiError } from "@/lib/api";
import type { AgentVersion as ApiAgentVersion } from "@/lib/api";
import { routes } from "@/lib/routes";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { VersionCompare } from "@/components/agents/VersionCompare";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/DropdownMenu";
import { GUARD_SELF_ATTRIBUTE } from "@/components/agents/stepper/useUnsavedChangesGuard";
import { publishWarningsOf, type PublishWarning } from "./agentCapabilities";
import { PublishWarnings } from "./PublishWarnings";
import { compareRefLabel, isDirtyDraftConflict, restoreVersion, type CompareRef } from "@/lib/agentVersions";

interface AgentVersion {
  agent_id: string;
  version: number;
  spec_hash: string;
  created_at: string;
  restored_from?: number | null;
}

/**
 * Where focus goes after a restore closes its confirm (the row menu that opened it is gone): the
 * "Version N published, restored from vM" notice, else the page's main (ConfirmDialog's default).
 */
export const RESTORE_NOTICE_ATTRIBUTE = "data-restore-notice";
function restoreFocusTarget(): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[${RESTORE_NOTICE_ATTRIBUTE}]`);
}

type RestoreState = { version: number; discard: boolean; busy: boolean; error: string | null };

export interface VersionRestoreOptions {
  agentId: string | undefined;
  /** The latest published version number (0 when none). */
  latest: number;
  /** The editor has edits that are not saved; a restore replaces them. */
  hasUnsavedEdits?: boolean;
  /** Called with the new version after a successful restore. */
  onRestored?: (version: ApiAgentVersion) => void;
}

export interface VersionRestore {
  /** Ask to restore `version` (opens the confirm). */
  start: (version: number) => void;
  /** The confirm dialog to render (null when closed). */
  dialog: React.ReactNode;
  /** "Version 5 published, restored from v1" after a restore. */
  notice: string | null;
  /** The restore response's publish `warnings` (AC.13; empty when none). */
  warnings: PublishWarning[];
  clearNotice: () => void;
}

/**
 * The P0.10 restore flow: a confirm stating the new version number, the dirty-draft 409 turned into
 * a "Discard draft and restore" confirm, and any other error (409 same spec, concurrent publish,
 * 422) shown inside the dialog. Used by the versions list and the compare Sheet.
 */
export function useVersionRestore({ agentId, latest, hasUnsavedEdits = false, onRestored }: VersionRestoreOptions): VersionRestore {
  const [restore, setRestore] = useState<RestoreState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<PublishWarning[]>([]);
  // Set when a restore succeeded: its confirm then sends focus to the notice, not back to its opener.
  const restoredRef = useRef(false);
  const skipOpenerReturn = useCallback(() => restoredRef.current, []);

  const run = async (state: RestoreState) => {
    if (!agentId) return;
    restoredRef.current = false;
    setRestore({ ...state, busy: true, error: null });
    try {
      const created = await restoreVersion(agentId, state.version, { discardDraft: state.discard });
      restoredRef.current = true;
      setRestore(null);
      setNotice(`Version ${created.version} published, restored from v${state.version}`);
      setWarnings(publishWarningsOf(created));
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

  const start = useCallback((version: number) => {
    setNotice(null);
    setWarnings([]);
    restoredRef.current = false;
    setRestore({ version, discard: false, busy: false, error: null });
  }, []);
  const clearNotice = useCallback(() => {
    setNotice(null);
    setWarnings([]);
  }, []);

  const dialog = restore ? (
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
        onConfirm={() => run(restore)}
        onCancel={() => setRestore(null)}
        fallbackFocus={restoreFocusTarget}
        skipOpenerReturn={skipOpenerReturn}
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
        onConfirm={() => run(restore)}
        onCancel={() => setRestore(null)}
        fallbackFocus={restoreFocusTarget}
        skipOpenerReturn={skipOpenerReturn}
      />
    )
  ) : null;

  return { start, dialog, notice, warnings, clearNotice };
}

interface AgentVersionsPanelProps {
  /** The caller may publish, so may restore an old version. */
  mayRestore?: boolean;
  versions: AgentVersion[];
  /** Enables compare and restore. Without it the panel is a read-only list. */
  agentId?: string;
  /** The editor has edits that are not saved; a restore replaces them. */
  hasUnsavedEdits?: boolean;
  /** Called with the new version after a successful restore. */
  onRestored?: (version: ApiAgentVersion) => void;
  /** Opens a compare (the editor's `?compare=` Sheet). Without it the compare shows in the panel. */
  onCompare?: (base: CompareRef, target: CompareRef) => void;
  /** The saved draft differs from the latest version: a "Draft" row leads the list. */
  draftChanged?: boolean;
  /**
   * Follows an in-app link through the navigation guard (the editor's unsaved-changes dialog). With
   * it, a plain click on "Test in chat" closes the row menu first and calls this; modified clicks
   * (new tab) stay ordinary link clicks. Without it the item is a plain link.
   */
  onNavigate?: (href: string) => void;
}

const ICON = { strokeWidth: ICON_STROKE_WIDTH, "aria-hidden": true } as const;

/**
 * The editor's versions column (decision "Version compare and restore"): rows `vN` newest first with
 * Latest, Draft and "restored from vM" badges, each with a menu (compare with latest, with the
 * draft or with another version, test in chat, restore). Two rows can be ticked and compared.
 * Narrow by design: no table, so it fits a 260 px column and a phone.
 */
export function AgentVersionsPanel({ versions, agentId, hasUnsavedEdits = false, onRestored, onCompare, draftChanged = false, onNavigate, mayRestore = true }: AgentVersionsPanelProps) {
  // The open row menu, so a guarded "Test in chat" can close it before the leave dialog opens.
  const [openMenu, setOpenMenu] = useState<CompareRef | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [inlineCompare, setInlineCompare] = useState<{ base: CompareRef; target: CompareRef } | null>(null);
  const latest = versions.reduce((max, v) => Math.max(max, v.version), 0);
  const restore = useVersionRestore({
    agentId,
    latest,
    hasUnsavedEdits,
    onRestored: (created) => {
      setSelected([]);
      onRestored?.(created);
    },
  });

  if (versions.length === 0) {
    return null;
  }

  const interactive = agentId !== undefined;

  const compare = (base: CompareRef, target: CompareRef) => {
    if (onCompare) onCompare(base, target);
    else setInlineCompare({ base, target });
  };

  const toggle = (version: number) => {
    setSelected((current) =>
      current.includes(version) ? current.filter((v) => v !== version) : current.length >= 2 ? current : [...current, version],
    );
  };

  const openSelectedCompare = () => {
    if (selected.length !== 2) return;
    const [a, b] = [...selected].sort((x, y) => x - y);
    compare(a, b);
  };

  // Compare `ref` with `other`, the older (or a version before the draft) as the base.
  const ordered = (ref: CompareRef, other: CompareRef): [CompareRef, CompareRef] => {
    if (ref === "draft") return [other, ref];
    if (other === "draft") return [ref, other];
    return ref < other ? [ref, other] : [other, ref];
  };

  // A compare reads the saved draft: with unsaved edits it would not show them, so it waits for a save.
  const draftLocked = hasUnsavedEdits;
  const draftHint = draftLocked ? "Save the draft to compare it." : null;

  const rowMenu = (ref: CompareRef) => {
    const label = ref === "draft" ? "the draft" : `version ${ref}`;
    const others: CompareRef[] = [...(ref !== "draft" ? (["draft"] as CompareRef[]) : []), ...versions.map((v) => v.version).filter((n) => n !== ref)];
    return (
      <DropdownMenu open={openMenu === ref} onOpenChange={(open) => setOpenMenu(open ? ref : null)}>
        <DropdownMenuTrigger asChild>
          <IconButton label={`Actions for ${label}`} size="sm" tooltip={false} icon={<MoreHorizontal {...ICON} />} />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {ref !== latest && latest > 0 ? (
            <DropdownMenuItem disabled={ref === "draft" && draftLocked} onSelect={() => compare(...ordered(ref, latest))}>
              Compare with latest
            </DropdownMenuItem>
          ) : null}
          {ref !== "draft" ? (
            <DropdownMenuItem disabled={draftLocked} onSelect={() => compare(ref, "draft")}>
              Compare with draft
            </DropdownMenuItem>
          ) : null}
          {others.length > 0 ? (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>Compare with…</DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                {others.map((other) => (
                  <DropdownMenuItem key={String(other)} disabled={draftLocked && (ref === "draft" || other === "draft")} onSelect={() => compare(...ordered(ref, other))}>
                    {compareRefLabel(other)}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          ) : null}
          {agentId ? (
            <DropdownMenuItem asChild>
              <Link
                href={routes.agentChat(agentId, { version: ref })}
                {...(onNavigate ? { [GUARD_SELF_ATTRIBUTE]: "" } : {})}
                onClick={(event) => {
                  if (!onNavigate || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  // Close the menu ourselves (a prevented click skips the menu's own close), then ask.
                  event.preventDefault();
                  setOpenMenu(null);
                  onNavigate(routes.agentChat(agentId, { version: ref }));
                }}
              >
                Test in chat
              </Link>
            </DropdownMenuItem>
          ) : null}
          {mayRestore && ref !== "draft" && ref !== latest ? <DropdownMenuItem onSelect={() => restore.start(ref)}>Restore this version</DropdownMenuItem> : null}
          {draftHint ? <p className="px-2 py-1 text-caption text-muted-foreground">{draftHint}</p> : null}
        </DropdownMenuContent>
      </DropdownMenu>
    );
  };

  return (
    <div data-testid="agent-versions" className="min-w-0">
      <h2 className="mb-2 text-title font-semibold text-foreground">Versions</h2>
      {interactive ? (
        <div className="mb-2 flex min-w-0 flex-wrap gap-2">
          {latest > 0 ? (
            <Button size="sm" variant="secondary" onClick={() => compare(latest, "draft")} disabled={draftLocked} aria-describedby={draftLocked ? "compare-draft-hint" : undefined}>
              Compare draft with latest
            </Button>
          ) : null}
          <Button size="sm" variant="primary" onClick={openSelectedCompare} disabled={selected.length !== 2} aria-describedby="compare-selected-hint">
            Compare selected
          </Button>
        </div>
      ) : null}
      {interactive && draftHint ? (
        <p id="compare-draft-hint" className="mb-1 text-caption text-warning-text">
          {draftHint}
        </p>
      ) : null}
      {interactive ? (
        <p id="compare-selected-hint" className="mb-2 text-caption text-muted-foreground">
          Select two versions to compare them ({selected.length} of 2 selected).
        </p>
      ) : null}
      {restore.notice ? (
        <p role="status" data-restore-notice="" tabIndex={-1} className="mb-3 rounded-md border border-border bg-muted p-2 text-label text-foreground outline-none">
          {restore.notice}
        </p>
      ) : null}
      {restore.notice ? <PublishWarnings warnings={restore.warnings} className="mb-3" /> : null}
      {inlineCompare && agentId ? (
        <div className="mb-4">
          <VersionCompare agentId={agentId} base={inlineCompare.base} target={inlineCompare.target} onClose={() => setInlineCompare(null)} />
        </div>
      ) : null}
      <ul aria-label="Published versions" className="min-w-0 divide-y divide-border rounded-md border border-border bg-surface">
        {interactive && draftChanged ? (
          <li data-testid="version-row-draft" className="flex min-w-0 items-start gap-2 px-2 py-2">
            <span className="size-4 shrink-0" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <p className="flex min-w-0 flex-wrap items-center gap-1.5 text-label font-medium text-foreground">
                Draft <Badge variant="warning">Unpublished changes</Badge>
              </p>
              <p className="text-caption text-muted-foreground">Saved, not published</p>
            </div>
            {rowMenu("draft")}
          </li>
        ) : null}
        {versions.map((version) => {
          const isSelected = selected.includes(version.version);
          return (
            <li key={`${version.agent_id}-${version.version}`} className="flex min-w-0 items-start gap-2 px-2 py-2">
              {interactive ? (
                <input
                  type="checkbox"
                  checked={isSelected}
                  disabled={!isSelected && selected.length >= 2}
                  onChange={() => toggle(version.version)}
                  aria-label={`Select version ${version.version} for compare`}
                  className="mt-0.5 size-4 shrink-0 accent-primary"
                />
              ) : null}
              <div className="min-w-0 flex-1">
                <p className="flex min-w-0 flex-wrap items-center gap-1.5 text-label font-medium text-foreground">
                  <span className="tabular-nums">
                    <span className="sr-only">Version </span>v{version.version}
                  </span>
                  {version.version === latest ? <Badge variant="success">Latest</Badge> : null}
                  {version.restored_from != null ? <span className="text-caption font-normal text-muted-foreground">restored from v{version.restored_from}</span> : null}
                </p>
                <dl className="flex min-w-0 flex-wrap gap-x-2 text-caption text-muted-foreground">
                  <dt className="sr-only">Hash</dt>
                  <dd className="font-mono">{version.spec_hash.slice(0, 8)}</dd>
                  <dt className="sr-only">Created</dt>
                  <dd className={cn("min-w-0 truncate tabular-nums")}>{new Date(version.created_at).toLocaleString()}</dd>
                </dl>
              </div>
              {interactive ? rowMenu(version.version) : null}
            </li>
          );
        })}
      </ul>
      <p className="mt-2 text-caption text-muted-foreground">
        Showing {versions.length} version{versions.length !== 1 ? "s" : ""}
      </p>
      {restore.dialog}
    </div>
  );
}
