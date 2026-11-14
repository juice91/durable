"use client";

import React from "react";
import { History } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { VersionCompare, compareTitle } from "../VersionCompare";
import type { VersionRestore } from "../AgentVersionsPanel";
import type { EditorCompare } from "./useEditorQuery";

export interface CompareSheetProps {
  agentId: string;
  /** The open compare (`?compare=a..b`), or null (closed). */
  compare: EditorCompare | null;
  /** The latest published version (it cannot be restored: it is already current). */
  latest: number;
  onClose: () => void;
  /** The editor's restore flow (confirm, dirty-draft 409, errors). */
  restore: VersionRestore;
  /** Close focus handling; `preventDefault()` skips the Sheet's own return (a restore moves focus to
   * its notice instead). */
  onCloseAutoFocus?: (event: Event) => void;
}

/** The sides of a compare that can be restored: published versions other than the latest. */
export function restorableSides(compare: EditorCompare, latest: number): number[] {
  const sides = [compare.base, compare.target].filter((ref): ref is number => typeof ref === "number" && ref !== latest);
  return Array.from(new Set(sides));
}

/**
 * The version compare as a wide Sheet addressed by `?compare=a..b` (decision "Version compare and
 * restore"): changed fields and the instructions diff (`VersionCompare`), with "Restore vN" for a
 * published side that is not the latest.
 */
export function CompareSheet({ agentId, compare, latest, onClose, restore, onCloseAutoFocus }: CompareSheetProps) {
  return (
    <Sheet open={compare !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      {compare ? (
        <SheetContent size="wide" aria-describedby={undefined} data-testid="compare-sheet" onCloseAutoFocus={onCloseAutoFocus}>
          <SheetHeader
            title={compareTitle(compare.base, compare.target)}
            actions={restorableSides(compare, latest).map((version) => (
              <Button key={version} size="sm" variant="secondary" onClick={() => restore.start(version)}>
                <History strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                Restore v{version}
              </Button>
            ))}
          />
          <SheetBody>
            <VersionCompare agentId={agentId} base={compare.base} target={compare.target} onClose={onClose} embedded />
          </SheetBody>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
