"use client";
// The page side of optimistic concurrency: run a save through `guard`; a 412 (ConflictError) is held
// as `conflict` instead of an error, and the page shows <ConflictNotice> with two actions:
//  - reload: hand the current version (as the API returned it with the 412) to the page, which puts it
//    in its editor, discarding local edits;
//  - overwrite: resend the same save with the current revision.
import { useCallback, useRef, useState } from "react";
import type { ConflictError } from "./api";
import { isConflict, rememberRevision } from "./revisions";

export interface ConflictState<T> {
  /** The refusal being shown, or null. */
  conflict: ConflictError | null;
  /** True while an overwrite is in flight. */
  overwriting: boolean;
  /**
   * Run `save`. A ConflictError is held (and `undefined` returned); other errors propagate to the
   * caller; success clears any earlier conflict.
   */
  guard: <R>(save: () => Promise<R>) => Promise<R | undefined>;
  /** Drop the notice and return the current resource the 412 carried (undefined without one). */
  reload: () => T | undefined;
  /** Resend the last guarded save with the current revision; a second conflict shows the notice again. */
  overwrite: () => Promise<void>;
  dismiss: () => void;
}

export function useConflict<T = unknown>(options: { onSaved?: (saved: unknown) => void } = {}): ConflictState<T> {
  // `onSaved` receives the result of an overwrite (the page applies it like any other save).
  const onSaved = useRef(options.onSaved);
  onSaved.current = options.onSaved;
  const [conflict, setConflict] = useState<ConflictError | null>(null);
  const [overwriting, setOverwriting] = useState(false);
  const lastSave = useRef<(() => Promise<unknown>) | null>(null);

  const guard = useCallback(async <R,>(save: () => Promise<R>): Promise<R | undefined> => {
    lastSave.current = save;
    try {
      const saved = await save();
      setConflict(null);
      return saved;
    } catch (error) {
      if (isConflict(error)) {
        setConflict(error);
        return undefined;
      }
      throw error;
    }
  }, []);

  const dismiss = useCallback(() => {
    setConflict(null);
    lastSave.current = null;
  }, []);

  const reload = useCallback((): T | undefined => {
    const held = conflict;
    if (held === null) return undefined;
    rememberRevision(held.key, held.currentRevision);
    dismiss();
    return held.current as T | undefined;
  }, [conflict, dismiss]);

  const overwrite = useCallback(async () => {
    const held = conflict;
    const save = lastSave.current;
    if (held === null || save === null) return;
    rememberRevision(held.key, held.currentRevision);
    setOverwriting(true);
    try {
      const saved = await guard(save);
      if (saved !== undefined) onSaved.current?.(saved);
    } finally {
      setOverwriting(false);
    }
  }, [conflict, guard]);

  return { conflict, overwriting, guard, reload, overwrite, dismiss };
}
