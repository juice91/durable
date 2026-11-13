"use client";

import React, { useCallback, useEffect, useId, useState } from "react";
import Link from "next/link";
import { cn } from "@/lib/cn";
import { RequestContextIcon } from "@/lib/icons";
import { routes } from "@/lib/routes";
import {
  deletePreset,
  listPresets,
  presetNameError,
  savePreset,
  type ContextPreset,
  type ContextVersionRef,
  type RequestContextInfo,
  type RequestContextValue,
} from "@/lib/requestContext";
import { useMediaQuery } from "@/components/frame/useMediaQuery";
import { Button, focusRing } from "@/components/ui/Button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/Popover";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { RequestContextForm, type ContextValidationState } from "./RequestContextForm";

/** Below 640 px the popover is a Sheet (layout rule 7). */
export const CONTEXT_SHEET_QUERY = "(max-width: 639.98px)";
/** The panel's id: the composer button controls it. */
export const CONTEXT_PANEL_ID = "playground-context-panel";
export const CONTEXT_TITLE = "Request context";

const STORAGE_UNAVAILABLE = "Browser storage is unavailable; the preset was not saved.";

/** Errors from a refused turn start (422) about the request context. */
export interface ContextTurnErrors {
  banner: string | null;
  fieldErrors: Record<string, string>;
}

export interface RequestContextPopoverProps {
  agentId: string;
  /** The chat's target: a version, "draft", or null for the latest. */
  versionRef: ContextVersionRef;
  /** The target's fields and schema; null while loading or when loading failed. */
  info: RequestContextInfo | null;
  loading: boolean;
  loadError: string | null;
  value: RequestContextValue;
  onChange: (value: RequestContextValue) => void;
  validation: ContextValidationState;
  onValidation: (state: ContextValidationState) => void;
  /** Errors from the last refused turn start (422), shown until the value changes. */
  turnErrors: ContextTurnErrors | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The selected preset (the one Update and Delete act on), or null. */
  presetName: string | null;
  onPresetNameChange: (name: string | null) => void;
  /** A notice about presets (loaded, saved, the preset in use), or null. */
  presetMessage: string | null;
  onPresetMessage: (message: string | null) => void;
  /** No agent yet: the button is disabled. */
  disabled?: boolean;
  /** Validation debounce; tests pass 0. */
  debounceMs?: number;
}

/** The button's accessible summary: "No context fields", "1 of 2 set", "… (problems)". */
export function contextSummary(fieldCount: number | null, valueCount: number, hasErrors: boolean): string {
  const base = fieldCount === null ? "Context" : fieldCount === 0 ? "No context fields" : `${valueCount} of ${fieldCount} set`;
  return hasErrors ? `${base} (problems)` : base;
}

function versionName(ref: ContextVersionRef): string {
  return ref === "draft" ? "the draft" : ref === null ? "the latest version" : `v${ref}`;
}

interface ContextButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  summary: string;
  /** The badge: the number of set fields, "!" when invalid, nothing when none are set. */
  badge: string | null;
  invalid: boolean;
}

/** The composer's key-icon button with its badge. */
const ContextButton = React.forwardRef<HTMLButtonElement, ContextButtonProps>(function ContextButton(
  { summary, badge, invalid, className, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      id="playground-context-toggle"
      aria-label={`${CONTEXT_TITLE}: ${summary}`}
      data-testid="composer-context-button"
      className={cn(
        "relative inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-fill-hover hover:text-foreground data-[state=open]:bg-fill-selected data-[state=open]:text-foreground disabled:pointer-events-none disabled:opacity-50",
        invalid && "text-destructive-text",
        focusRing,
        className,
      )}
      {...props}
    >
      <RequestContextIcon aria-hidden="true" className="size-icon" />
      {badge ? (
        <span
          aria-hidden="true"
          data-testid="composer-context-badge"
          className={cn(
            "absolute -right-0.5 -top-0.5 min-w-4 rounded-full px-1 text-center text-badge font-semibold leading-4",
            invalid ? "bg-destructive text-destructive-foreground" : "bg-primary text-primary-foreground",
          )}
        >
          {badge}
        </span>
      ) : null}
    </button>
  );
});

/**
 * The composer's request context control (decision "Where Playground features go"): a key-icon
 * button with a badge (set fields, or `!` when invalid) opening a 480 px popover, a Sheet below
 * 640 px. Inside: the schema form with its JSON mode and server validation, named presets per agent
 * (select, Save as…, Update, Delete), Clear, the editor link and the JSON Schema.
 */
export function RequestContextPopover(props: RequestContextPopoverProps) {
  const { open, onOpenChange, info, value, validation, turnErrors, disabled = false } = props;
  const sheet = useMediaQuery(CONTEXT_SHEET_QUERY, false);
  const fieldCount = info ? info.fields.length : null;
  const valueCount = info
    ? Object.entries(value).filter(([key, item]) => item !== undefined && item !== null && item !== "" && info.fields.some((field) => field.name === key)).length
    : 0;
  const hasErrors = Boolean(turnErrors) || validation.status === "invalid";
  const summary = contextSummary(fieldCount, valueCount, hasErrors);
  const badge = hasErrors ? "!" : valueCount > 0 ? String(valueCount) : null;
  const buttonProps = { summary, badge, invalid: hasErrors, disabled, "aria-controls": CONTEXT_PANEL_ID };

  if (sheet) {
    return (
      <>
        <ContextButton {...buttonProps} aria-haspopup="dialog" aria-expanded={open} data-state={open ? "open" : "closed"} onClick={() => onOpenChange(!open)} />
        <Sheet open={open} onOpenChange={onOpenChange}>
          <SheetContent id={CONTEXT_PANEL_ID} size="md" aria-describedby={undefined} data-testid="request-context-sheet">
            <SheetHeader title={CONTEXT_TITLE} />
            <SheetBody>
              <ContextBody {...props} />
            </SheetBody>
          </SheetContent>
        </Sheet>
      </>
    );
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <ContextButton {...buttonProps} />
      </PopoverTrigger>
      <PopoverContent
        id={CONTEXT_PANEL_ID}
        aria-label={CONTEXT_TITLE}
        data-testid="request-context-popover"
        side="top"
        align="start"
        className="flex max-h-[var(--radix-popover-content-available-height)] w-80 flex-col overflow-y-auto p-0 md:w-[480px]"
      >
        <div className="flex min-h-header shrink-0 items-center border-b border-border px-4 py-2">
          <h2 className="text-title font-semibold text-foreground">{CONTEXT_TITLE}</h2>
        </div>
        <div className="min-h-0 p-4">
          <ContextBody {...props} />
        </div>
      </PopoverContent>
    </Popover>
  );
}

function ContextBody({
  agentId,
  versionRef,
  info,
  loading,
  loadError,
  value,
  onChange,
  validation,
  onValidation,
  turnErrors,
  presetName,
  onPresetNameChange,
  presetMessage,
  onPresetMessage,
  debounceMs,
}: RequestContextPopoverProps) {
  const turnFieldErrors = Object.entries(turnErrors?.fieldErrors ?? {});
  const showForm = info !== null && (info.fields.length > 0 || Object.keys(value).length > 0);
  return (
    <section aria-label={`${CONTEXT_TITLE} values`} data-testid="playground-context-panel" className="min-w-0 space-y-4">
      <p className="text-label text-muted-foreground">
        Sent with every turn and checked against {versionName(versionRef)}&apos;s context fields.
      </p>

      {turnErrors && (turnErrors.banner || turnFieldErrors.length > 0) ? (
        <div role="alert" data-testid="context-turn-errors" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-label text-destructive-text">
          <p className="font-medium">The message was not sent: the request context was refused.</p>
          {turnErrors.banner ? <p className="mt-1">{turnErrors.banner}</p> : null}
          {turnFieldErrors.length > 0 ? (
            <ul className="mt-1 space-y-0.5 text-caption">
              {turnFieldErrors.map(([field, message]) => (
                <li key={field}>
                  <span className="font-mono">{field}</span>: {message}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <PresetControls
        agentId={agentId}
        versionRef={versionRef}
        validation={validation}
        fieldsKnown={info !== null && !loading}
        presetName={presetName}
        onPresetNameChange={onPresetNameChange}
        presetMessage={presetMessage}
        onPresetMessage={onPresetMessage}
        onLoad={onChange}
      />

      {loading ? (
        <p className="text-label text-muted-foreground">Loading context fields…</p>
      ) : loadError ? (
        <p role="alert" data-testid="context-load-error" className="text-label text-destructive-text">
          {loadError}
        </p>
      ) : info && info.fields.length === 0 ? (
        <p data-testid="context-no-fields" className="text-label text-muted-foreground">
          {versionRef === "draft" ? "The draft" : versionRef === null ? "This version" : `Version ${versionRef}`} declares no context fields.
        </p>
      ) : null}

      {/* With no fields the form still shows while the value has keys (a remembered or preset value
          from another version), so they can be seen and removed. */}
      {showForm && info ? (
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
      ) : null}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border pt-3">
        <Button size="sm" variant="ghost" onClick={() => onChange({})} disabled={Object.keys(value).length === 0}>
          Clear
        </Button>
        <Link
          href={routes.agentEditor(agentId, { step: "request-context" })}
          className={cn("rounded-sm text-label text-info-text underline-offset-4 hover:underline", focusRing)}
        >
          Fields are defined in the editor
        </Link>
      </div>

      {info ? (
        <details className="text-label">
          <summary className={cn("cursor-pointer rounded-sm text-muted-foreground hover:text-foreground", focusRing)}>JSON Schema</summary>
          <pre data-testid="context-json-schema" className="mt-2 max-h-48 overflow-auto rounded-md bg-fill-hover p-3 font-mono text-caption text-foreground">
            {JSON.stringify(info.schema, null, 2)}
          </pre>
        </details>
      ) : null}

      <p data-testid="saved-context-storage-note" className="text-caption text-muted-foreground">
        Presets are stored unencrypted in this browser&apos;s local storage. Don&apos;t save secrets.
      </p>
    </section>
  );
}

const selectClass = cn(
  "h-8 min-w-0 flex-1 rounded-md border border-border bg-surface px-2 text-label text-foreground disabled:opacity-50",
  focusRing,
);

/** Named presets of this agent: pick one to load it; Save as…, Update, Delete. */
function PresetControls({
  agentId,
  versionRef,
  validation,
  fieldsKnown,
  presetName,
  onPresetNameChange,
  presetMessage,
  onPresetMessage,
  onLoad,
}: {
  agentId: string;
  versionRef: ContextVersionRef;
  validation: ContextValidationState;
  /** The target version's fields have loaded: a validation result can be trusted for it. */
  fieldsKnown: boolean;
  presetName: string | null;
  onPresetNameChange: (name: string | null) => void;
  presetMessage: string | null;
  onPresetMessage: (message: string | null) => void;
  onLoad: (value: RequestContextValue) => void;
}) {
  const nameId = useId();
  const [presets, setPresets] = useState<ContextPreset[]>([]);
  const [naming, setNaming] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const refresh = useCallback(() => setPresets(listPresets(agentId)), [agentId]);
  useEffect(refresh, [refresh]);

  // Only a value the server accepted for the current version's fields may be saved.
  const accepted = fieldsKnown && validation.status === "valid" ? validation.accepted : null;
  const selected = presetName !== null && presets.some((preset) => preset.name === presetName) ? presetName : null;

  const choose = (name: string) => {
    const preset = presets.find((item) => item.name === name);
    if (!preset) {
      onPresetNameChange(null);
      return;
    }
    onLoad(preset.value);
    onPresetNameChange(preset.name);
    onPresetMessage(`Loaded preset “${preset.name}”.`);
  };

  const save = (name: string, verb: "Saved" | "Updated") => {
    if (!accepted) return false;
    const problem = presetNameError(name);
    if (problem) {
      setNameError(problem);
      return false;
    }
    const saved = savePreset(agentId, name, accepted, versionRef);
    if (!saved) {
      onPresetMessage(STORAGE_UNAVAILABLE);
      return false;
    }
    refresh();
    onPresetNameChange(saved.name);
    onPresetMessage(`${verb} preset “${saved.name}”.`);
    return true;
  };

  const remove = () => {
    if (!selected) return;
    if (!deletePreset(agentId, selected)) {
      onPresetMessage("Browser storage is unavailable; the preset was not deleted.");
      return;
    }
    refresh();
    onPresetNameChange(null);
    onPresetMessage(`Deleted preset “${selected}”.`);
  };

  return (
    <div className="space-y-2" data-testid="context-presets">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Preset"
          className={selectClass}
          value={selected ?? ""}
          onChange={(event) => choose(event.target.value)}
          disabled={presets.length === 0}
        >
          <option value="">{presets.length === 0 ? "No presets yet" : "Choose a preset…"}</option>
          {presets.map((preset) => (
            <option key={preset.name} value={preset.name}>
              {preset.name}
            </option>
          ))}
        </select>
        <div className="flex flex-wrap gap-1">
          <Button
            size="sm"
            onClick={() => {
              setNaming(true);
              setDraftName(selected ?? "");
              setNameError(null);
            }}
            disabled={!accepted || naming}
          >
            Save as…
          </Button>
          <Button size="sm" onClick={() => selected && save(selected, "Updated")} disabled={!accepted || !selected}>
            Update
          </Button>
          <Button size="sm" variant="ghost" onClick={remove} disabled={!selected}>
            Delete
          </Button>
        </div>
      </div>

      {naming ? (
        <form
          className="flex flex-wrap items-start gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            // The popover renders inside the composer's form in React's tree (portals bubble): a
            // submit here must never reach it and send the message.
            event.stopPropagation();
            if (save(draftName, "Saved")) setNaming(false);
          }}
        >
          <div className="min-w-0 flex-1">
            <input
              id={nameId}
              aria-label="Preset name"
              autoFocus
              value={draftName}
              maxLength={80}
              onChange={(event) => {
                setDraftName(event.target.value);
                setNameError(null);
              }}
              aria-invalid={Boolean(nameError)}
              aria-describedby={nameError ? `${nameId}-error` : undefined}
              className={cn("h-8 w-full rounded-md border border-border bg-surface px-2 text-label text-foreground", focusRing)}
            />
            {nameError ? (
              <p id={`${nameId}-error`} role="alert" className="mt-1 text-caption text-destructive-text">
                {nameError}
              </p>
            ) : null}
          </div>
          <Button size="sm" variant="primary" type="submit" disabled={!accepted}>
            Save preset
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setNaming(false)}>
            Cancel
          </Button>
        </form>
      ) : null}

      {!accepted && validation.status !== "checking" && validation.status !== "idle" ? (
        <p className="text-caption text-muted-foreground">Only a valid context can be saved as a preset.</p>
      ) : null}
      {presetMessage ? (
        <p role="status" data-testid="context-save-notice" className="text-caption text-muted-foreground">
          {presetMessage}
        </p>
      ) : null}
    </div>
  );
}
