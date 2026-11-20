"use client";

import React from "react";
import { History } from "lucide-react";
import type { AgentVersion } from "@/lib/api";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { versionFromSelectValue, versionSelectValue, type VersionRef } from "@/components/chat/runOptions";
import { ComposerChipSelect } from "./ComposerChip";

export const VERSION_ERROR_ID = "playground-version-error";

export interface ComposerVersionChipProps {
  /** The agent's published versions; null while loading. */
  versions: AgentVersion[] | null;
  latestVersion: number | null;
  /** The effective target (an unpublished agent is always "draft"). */
  versionRef: VersionRef;
  onChange: (ref: VersionRef) => void;
  /** Draft is listed first when it holds unpublished changes (else last). */
  draftDiffers: boolean;
  error: string | null;
  disabled?: boolean;
}

/**
 * "Latest · v3" when nothing is pinned (the chat follows new versions), "v3" for a pinned version,
 * "Draft". The pinned and the latest v3 never read the same.
 */
export function versionChipLabel(ref: VersionRef, latestVersion: number | null): string {
  if (ref === "draft") return "Draft";
  if (ref === null) return latestVersion === null ? "Latest" : `Latest · v${latestVersion}`;
  return `v${ref}`;
}

/** The composer's version chip: Latest, each published version newest first, and Draft. */
export function ComposerVersionChip({ versions, latestVersion, versionRef, onChange, draftDiffers, error, disabled = false }: ComposerVersionChipProps) {
  const listed = [...(versions ?? [])].sort((a, b) => b.version - a.version);
  const draft = <option value="draft">Draft (unpublished)</option>;
  return (
    <ComposerChipSelect
      id="playground-version"
      testId="composer-chip-version"
      label="Version"
      display={versionChipLabel(versionRef, latestVersion)}
      icon={<History strokeWidth={ICON_STROKE_WIDTH} />}
      invalid={Boolean(error)}
      errorId={VERSION_ERROR_ID}
      value={versionSelectValue(versionRef)}
      disabled={disabled}
      onValueChange={(v) => onChange(versionFromSelectValue(v))}
    >
      {draftDiffers && latestVersion !== null ? draft : null}
      {latestVersion !== null ? <option value="latest">Latest (v{latestVersion})</option> : null}
      {listed.map((v) => (
        <option key={v.version} value={`v${v.version}`}>
          v{v.version}
          {v.restored_from ? ` (restored from v${v.restored_from})` : ""}
        </option>
      ))}
      {typeof versionRef === "number" && !listed.some((v) => v.version === versionRef) ? <option value={`v${versionRef}`}>v{versionRef}</option> : null}
      {draftDiffers && latestVersion !== null ? null : draft}
    </ComposerChipSelect>
  );
}
