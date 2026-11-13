"use client";

import React from "react";
import {
  RequestContextForm,
  type ContextValidationState,
} from "@/components/requestContext";
import type { RequestContextInfo, RequestContextValue } from "@/lib/requestContext";
import type { TurnOptionErrors, VersionRef } from "./runOptions";

export interface ContextPanelProps {
  agentId: string;
  versionRef: VersionRef;
  /** The target's fields and schema; null while loading or when loading failed. */
  info: RequestContextInfo | null;
  loading: boolean;
  loadError: string | null;
  value: RequestContextValue;
  onChange: (value: RequestContextValue) => void;
  onValidation: (state: ContextValidationState) => void;
  /** Errors from the last refused turn start (422), shown until the value changes. */
  turnErrors: TurnOptionErrors["context"];
  /** A preset saved on the Request Context page exists for this agent. */
  presetAvailable: boolean;
  onLoadPreset: () => void;
  onSavePreset: () => void;
  canSavePreset: boolean;
  presetMessage: string | null;
  onClear: () => void;
  /** Validation debounce; tests pass 0. */
  debounceMs?: number;
}

const buttonClass =
  "rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";

/** The request context sent with each turn: a form from the target version's fields, with presets. */
export function ContextPanel({
  agentId,
  versionRef,
  info,
  loading,
  loadError,
  value,
  onChange,
  onValidation,
  turnErrors,
  presetAvailable,
  onLoadPreset,
  onSavePreset,
  canSavePreset,
  presetMessage,
  onClear,
  debounceMs,
}: ContextPanelProps) {
  const turnFieldErrors = Object.entries(turnErrors?.fieldErrors ?? {});
  return (
    <section
      id="playground-context-panel"
      aria-label="Request context"
      data-testid="playground-context-panel"
      className="mt-4 max-h-80 overflow-y-auto rounded-lg border border-border bg-card p-4 text-card-foreground"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          Sent with every turn and checked against{" "}
          {versionRef === "draft" ? "the draft" : versionRef === null ? "the latest version" : `v${versionRef}`}
          {"'s"} context fields.
        </p>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={buttonClass} onClick={onLoadPreset} disabled={!presetAvailable}>
            Load saved preset
          </button>
          <button type="button" className={buttonClass} onClick={onSavePreset} disabled={!canSavePreset}>
            Save as preset
          </button>
          <button type="button" className={buttonClass} onClick={onClear} disabled={Object.keys(value).length === 0}>
            Clear
          </button>
        </div>
      </div>
      {presetMessage && (
        <p role="status" className="mb-3 text-xs text-muted-foreground">
          {presetMessage}
        </p>
      )}

      {turnErrors && (turnErrors.banner || turnFieldErrors.length > 0) && (
        <div
          role="alert"
          data-testid="context-turn-errors"
          className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
        >
          <p className="font-medium">The message was not sent: the request context was refused.</p>
          {turnErrors.banner && <p className="mt-1">{turnErrors.banner}</p>}
          {turnFieldErrors.length > 0 && (
            <ul className="mt-1 space-y-0.5 text-xs">
              {turnFieldErrors.map(([field, message]) => (
                <li key={field}>
                  <span className="font-mono">{field}</span>: {message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading context fields…</p>
      ) : loadError ? (
        <p role="alert" className="text-sm text-destructive">
          {loadError}
        </p>
      ) : info ? (
        info.fields.length === 0 ? (
          <p className="text-sm text-muted-foreground">This version declares no context fields.</p>
        ) : (
          <RequestContextForm
            key={`${agentId}:${String(versionRef)}`}
            agentId={agentId}
            version={versionRef}
            schema={info.schema}
            value={value}
            onChange={onChange}
            onValidation={onValidation}
            idPrefix="playground-context"
            {...(debounceMs === undefined ? {} : { debounceMs })}
          />
        )
      ) : null}
    </section>
  );
}
