"use client";
import React from "react";
import { Button } from "@/components/ui/Button";
import type { ConflictState } from "@/lib/useConflict";

/**
 * The notice a page shows while a save was refused because someone else changed the resource
 * (412 `revision_mismatch`). 重新載入 reloads the current version into the editor, discarding local
 * edits; 覆寫 resends the save on top of it. Renders nothing without a conflict.
 */
export function ConflictNotice<T>({ state, onReload, className = "" }: { state: ConflictState<T>; onReload: (current: T | undefined) => void; className?: string }) {
  if (state.conflict === null) return null;
  return (
    <div role="alert" data-testid="conflict-notice" className={`flex flex-wrap items-center gap-3 rounded-md border border-warning/40 bg-warning/10 p-3 text-label text-warning-text ${className}`}>
      <p className="min-w-0 flex-1">這份內容已被他人修改</p>
      <Button size="sm" variant="ghost" data-testid="conflict-reload" onClick={() => onReload(state.reload())} disabled={state.overwriting}>
        重新載入
      </Button>
      <Button size="sm" variant="primary" data-testid="conflict-overwrite" onClick={() => void state.overwrite()} disabled={state.overwriting}>
        覆寫
      </Button>
    </div>
  );
}
