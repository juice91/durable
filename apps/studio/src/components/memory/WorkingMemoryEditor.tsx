"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  conflictMessage,
  deleteWorkingMemory,
  getWorkingMemory,
  noteText,
  parseNoteText,
  putWorkingMemory,
  type WorkingMemoryNote,
  type WorkingMemoryTarget,
  errorStatus,
} from "@/lib/memory";
import { cn } from "@/lib/cn";
import { primeWorkingMemory, sharedWorkingMemory } from "@/lib/workingMemoryCache";
import { Button, focusRing } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toaster";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

/** An edit in progress: the text, what it started from, and the note version it is based on. */
export interface WorkingMemoryDraft {
  /** The note it edits (the target, serialised): a draft applies only to its own note. */
  key: string;
  text: string;
  /** The text the edit started from ("" for a new note): `text !== original` is unsaved. */
  original: string;
  baseVersion: number;
}

/**
 * Where the edit lives when it must outlive the editor (the chat keeps it above the panes, so a
 * closed Sheet, a hidden card or a remount at 1024 px does not lose it).
 */
export interface WorkingMemoryDraftStore {
  draft: WorkingMemoryDraft | null;
  setDraft: (draft: WorkingMemoryDraft | null) => void;
}

/** Whether a draft holds unsaved text. */
export function draftDirty(draft: WorkingMemoryDraft | null | undefined): boolean {
  return Boolean(draft && draft.text !== draft.original);
}

export function targetKeyOf(target: WorkingMemoryTarget | null): string | null {
  return target ? JSON.stringify(target) : null;
}

export interface WorkingMemoryEditorProps {
  /** The note to show; null with `unavailable` explaining why there is none to show. */
  target: WorkingMemoryTarget | null;
  unavailable?: string | null;
  /**
   * The shape the API validates a PUT against (the latest published `memory.working`): Markdown
   * template or JSON schema. Edits use it, never the stored note's format: a note stored before a
   * template ↔ schema switch is shown read-only (`memory-working-format-mismatch`).
   */
  format: "markdown" | "json";
  /** What "Create" starts from: the agent's template (Markdown) or `{}` (JSON). */
  starter: string;
  /** Why edits would be refused (the API checks the latest published version), or null. */
  editBlocked?: string | null;
  /** The caller may change the note (the server's decision on the agent). */
  mayChange?: boolean;
  /** Bump to reload (a `memory_updated` event was seen). */
  refreshKey?: number;
  onDirtyChange?: (dirty: boolean) => void;
  /** Keep the edit outside the editor (it then survives an unmount); default: local state. */
  draftStore?: WorkingMemoryDraftStore;
  /** A line under the heading ("Shared by every conversation of alice"). */
  hint?: React.ReactNode;
  /** Fewer rows in the edit box (the threads pane's card). */
  compact?: boolean;
  /** The heading level: 3 in the card (under "Memory"), 2 on the Memory tab. */
  headingLevel?: 2 | 3;
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatDateTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

const FORMAT_NAME = { markdown: "Markdown (template)", json: "JSON (schema)" } as const;

/** Why a stored note cannot be edited under the published format, or null when they match. */
export function formatMismatchReason(noteFormat: "markdown" | "json", format: "markdown" | "json"): string | null {
  if (noteFormat === format) return null;
  return (
    `This note is stored as ${FORMAT_NAME[noteFormat]}, but the latest published version's working memory is ` +
    `${FORMAT_NAME[format]}, so a save would be refused. It is shown read-only: clear it to start a new note in the published format.`
  );
}

function updatedLine(note: WorkingMemoryNote): string {
  const by = note.updated_by_run_id ? "by the agent" : "by a person";
  return `Version ${note.version}, updated ${by} ${formatDateTime(note.updated_at)}`;
}

/**
 * One working memory note: view, edit (saved with `If-Match` on the version the edit started from,
 * so an edit never overwrites a newer note: the 409 says so and Reload fetches it), create, and
 * clear (confirmed). JSON schema mode edits the object as JSON; template mode edits Markdown.
 */
export function WorkingMemoryEditor({
  target,
  unavailable = null,
  format,
  starter,
  editBlocked = null,
  refreshKey = 0,
  onDirtyChange,
  draftStore,
  hint,
  compact = false,
  headingLevel = 3,
  mayChange = true,
}: WorkingMemoryEditorProps) {
  const [note, setNote] = useState<WorkingMemoryNote | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [localEdit, setLocalEdit] = useState<WorkingMemoryDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);
  const textareaId = useId();
  const errorId = useId();
  const requestRef = useRef(0);
  const targetKey = targetKeyOf(target);
  // What a save is validated against: the published format (the stored note may predate a switch).
  const noteFormat = format;
  // A note in the other format is read-only (only when edits are otherwise allowed: a blocked edit
  // already says why).
  const mismatch = note && !editBlocked ? formatMismatchReason(note.format, format) : null;
  const rawEdit = draftStore ? draftStore.draft : localEdit;
  const setEdit = draftStore ? draftStore.setDraft : setLocalEdit;
  // A draft of another note does not apply here, nor one of a note that cannot be edited.
  const edit = rawEdit && targetKey !== null && rawEdit.key === targetKey && !mismatch ? rawEdit : null;
  const editing = edit !== null;
  const draft = edit?.text ?? "";
  const original = edit?.original ?? "";
  const baseVersion = edit?.baseVersion ?? 0;
  const begin = (text: string, from: string, version: number) => {
    if (targetKey !== null) setEdit({ key: targetKey, text, original: from, baseVersion: version });
  };
  const setDraftText = (text: string) => {
    if (edit) setEdit({ ...edit, text });
  };
  const endEdit = () => setEdit(null);

  // One read per note and refresh, shared with every other editor of it (lib/workingMemoryCache):
  // `force` skips a cached answer (a write was seen, Retry, Reload).
  const load = useCallback(async (force = false): Promise<WorkingMemoryNote | null | undefined> => {
    const request = ++requestRef.current;
    if (!target || targetKey === null) return undefined;
    setLoading(true);
    setLoadError(null);
    try {
      const loaded = await sharedWorkingMemory(targetKey, () => getWorkingMemory(target), { force });
      if (request !== requestRef.current) return undefined;
      setNote(loaded);
      return loaded;
    } catch (error) {
      if (request === requestRef.current) setLoadError(messageOf(error, "Failed to load working memory."));
      return undefined;
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
    // targetKey stands for target (a new object each render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetKey]);

  // Another note: load it (a draft applies only to its own note, see `edit`; a lifted draft of this
  // note comes back after a remount). A memory write was seen (`refreshKey`): read it again. An edit
  // in progress keeps its text and base version, so saving it against a newer note answers 409
  // instead of overwriting it. One effect, so a change of both is one read, and its development
  // re-run (same target and refresh) none.
  const seenRef = useRef<{ key: string | null; refresh: number } | null>(null);
  useEffect(() => {
    const seen = seenRef.current;
    seenRef.current = { key: targetKey, refresh: refreshKey };
    if (seen && seen.key === targetKey && seen.refresh === refreshKey) return;
    if (!seen || seen.key !== targetKey) {
      setNote(null);
      setSaveError(null);
      setConflict(null);
    }
    void load(Boolean(seen) && seen?.refresh !== refreshKey);
  }, [targetKey, refreshKey, load]);

  const dirty = editing && draft !== original;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  // A local edit dies with the editor; a lifted one (draftStore) stays dirty until saved or discarded.
  const lifted = Boolean(draftStore);
  useEffect(() => () => {
    if (!lifted) onDirtyChange?.(false);
  }, [lifted, onDirtyChange]);

  const startEdit = () => {
    const text = note ? noteText(note.content) : starter;
    begin(text, note ? text : "", note?.version ?? 0);
    setSaveError(null);
    setConflict(null);
  };

  const cancelEdit = () => {
    endEdit();
    setSaveError(null);
    setConflict(null);
  };

  const save = async () => {
    if (!target || saving) return;
    const parsed = parseNoteText(draft, noteFormat);
    if ("error" in parsed) {
      setSaveError(parsed.error);
      return;
    }
    setSaving(true);
    setSaveError(null);
    setConflict(null);
    try {
      const saved = await putWorkingMemory(target, parsed.content, baseVersion);
      if (targetKey !== null) primeWorkingMemory(targetKey, saved);
      setNote(saved);
      endEdit();
      toast.success("Working memory saved");
    } catch (error) {
      if (errorStatus(error) === 409) setConflict(conflictMessage(messageOf(error, "")));
      else setSaveError(messageOf(error, "Failed to save working memory."));
    } finally {
      setSaving(false);
    }
  };

  // After a conflict: load the newer note into the editor (the edit has to be applied again).
  const reloadLatest = async () => {
    const latest = await load(true);
    if (latest === undefined) return;
    if (latest && formatMismatchReason(latest.format, format)) {
      // The newer note is in the other format: nothing to edit (the read-only view explains).
      endEdit();
      setConflict(null);
      setSaveError(null);
      return;
    }
    const text = latest ? noteText(latest.content) : starter;
    begin(text, latest ? text : "", latest?.version ?? 0);
    setConflict(null);
    setSaveError(null);
  };

  const clear = async () => {
    if (!target || !note) return;
    setClearing(true);
    setClearError(null);
    try {
      await deleteWorkingMemory(target, note.version);
      if (targetKey !== null) primeWorkingMemory(targetKey, null);
      setNote(null);
      endEdit();
      setConfirmClear(false);
      toast.success("Working memory cleared");
    } catch (error) {
      if (errorStatus(error) === 409) {
        setClearError(conflictMessage(messageOf(error, "")));
        void load(true);
      } else setClearError(messageOf(error, "Failed to clear working memory."));
    } finally {
      setClearing(false);
    }
  };

  const Heading = headingLevel === 2 ? "h2" : "h3";
  const headingClass = headingLevel === 2 ? "text-title font-semibold text-foreground" : "text-label font-semibold text-foreground";
  const stale = editing && note !== null && note.version !== baseVersion && !conflict;

  return (
    <section data-testid="memory-working" aria-label="Working memory" className="min-w-0 space-y-2">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <Heading className={headingClass}>Working memory</Heading>
        {mayChange && target && !editing && !loading && !loadError ? (
          <div className="flex shrink-0 gap-1">
            <Button size="sm" onClick={startEdit} disabled={Boolean(editBlocked) || Boolean(mismatch)} data-testid={note ? "memory-working-edit" : "memory-working-create"}>
              {note ? "Edit" : "Create"}
            </Button>
            {note ? (
              <Button size="sm" variant="ghost" onClick={() => setConfirmClear(true)} data-testid="memory-working-clear">
                Clear
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
      {hint ? <p className="text-caption text-muted-foreground">{hint}</p> : null}
      {mismatch && target && !loadError ? (
        <p data-testid="memory-working-format-mismatch" className="text-caption text-warning-text">
          {mismatch}
        </p>
      ) : null}
      {editBlocked && target && !editing ? (
        <p data-testid="memory-working-blocked" className="text-caption text-muted-foreground">
          {editBlocked}
        </p>
      ) : null}

      {!target ? (
        <p data-testid="memory-working-unavailable" className="text-label text-muted-foreground">
          {unavailable ?? "No working memory to show."}
        </p>
      ) : loadError ? (
        <div className="space-y-1">
          <p role="alert" data-testid="memory-working-error" className="text-label text-destructive-text">
            {loadError}
          </p>
          <Button size="sm" onClick={() => void load(true)}>
            Retry
          </Button>
        </div>
      ) : editing ? (
        <div className="min-w-0 space-y-2">
          <label htmlFor={textareaId} className="sr-only">
            {noteFormat === "json" ? "Working memory (JSON)" : "Working memory (Markdown)"}
          </label>
          <textarea
            id={textareaId}
            data-testid="memory-working-textarea"
            value={draft}
            onChange={(event) => {
              setDraftText(event.target.value);
              setSaveError(null);
            }}
            rows={compact ? 8 : 14}
            spellCheck={noteFormat !== "json"}
            aria-invalid={Boolean(saveError)}
            aria-describedby={saveError || conflict ? errorId : undefined}
            className={cn(
              "w-full min-w-0 resize-y rounded-md border border-input bg-surface px-2 py-1.5 font-mono text-caption text-foreground",
              focusRing,
            )}
          />
          <p className="text-caption text-muted-foreground">
            {noteFormat === "json" ? "A JSON object, checked against the agent's schema when saved." : "Markdown. The agent reads it at the start of every turn."}
          </p>
          {stale ? (
            <p role="status" data-testid="memory-working-stale" className="text-caption text-warning-text">
              The note changed (version {note?.version}) while you were editing. Saving will be refused; reload to see it.
            </p>
          ) : null}
          {saveError ? (
            <p id={errorId} role="alert" data-testid="memory-working-save-error" className="text-caption text-destructive-text">
              {saveError}
            </p>
          ) : null}
          {conflict ? (
            <div id={errorId} role="alert" data-testid="memory-working-conflict" className="space-y-1 rounded-md border border-warning/40 bg-warning/10 p-2 text-caption text-warning-text">
              <p>{conflict}</p>
              <p>Copy your edit if you want to keep it: Reload replaces it with the newer note.</p>
            </div>
          ) : null}
          <div className="flex flex-wrap gap-1">
            <Button size="sm" variant="primary" onClick={() => void save()} disabled={saving || Boolean(conflict)} data-testid="memory-working-save">
              {saving ? "Saving…" : "Save"}
            </Button>
            {conflict || stale ? (
              <Button size="sm" onClick={() => void reloadLatest()} data-testid="memory-working-reload">
                Reload
              </Button>
            ) : null}
            <Button size="sm" variant="ghost" onClick={cancelEdit} disabled={saving} data-testid="memory-working-cancel">
              Cancel
            </Button>
          </div>
        </div>
      ) : loading && !note ? (
        <p role="status" className="text-label text-muted-foreground">
          Loading working memory…
        </p>
      ) : note ? (
        <div className="min-w-0 space-y-1">
          <pre
            data-testid="memory-working-content"
            className={cn("overflow-auto whitespace-pre-wrap break-words rounded-md bg-fill-hover p-2 font-mono text-caption text-foreground", compact ? "max-h-48" : "max-h-96")}
          >
            {noteText(note.content) || " "}
          </pre>
          <p className="text-caption text-muted-foreground">{updatedLine(note)}</p>
        </div>
      ) : (
        <p data-testid="memory-working-empty" className="text-label text-muted-foreground">
          No working memory yet. The agent writes it as it learns; Create starts one by hand.
        </p>
      )}

      {confirmClear && note ? (
        <ConfirmDialog
          title="Clear working memory?"
          description="The note is deleted. The agent starts the next turn without it and may write a new one."
          confirmLabel="Clear"
          destructive
          busy={clearing}
          error={clearError}
          onConfirm={() => void clear()}
          onCancel={() => {
            setConfirmClear(false);
            setClearError(null);
          }}
        />
      ) : null}
    </section>
  );
}
