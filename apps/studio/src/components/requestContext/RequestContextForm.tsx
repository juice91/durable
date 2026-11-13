"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { JsonSchemaForm } from "@/components/tools/JsonSchemaForm";
import type { JsonSchema } from "@/lib/tools";
import {
  RequestContextValidationError,
  formatContextJson,
  normalizeContext,
  parseContextJson,
  validateRequestContext,
  type ContextVersionRef,
  type RequestContextValue,
} from "@/lib/requestContext";

export type ContextValidationStatus = "idle" | "checking" | "valid" | "invalid" | "error";

export interface ContextValidationState {
  status: ContextValidationStatus;
  /** The context the server accepted ("valid" only): declared fields only, ready to send on a run. */
  accepted: RequestContextValue | null;
  fieldErrors: Record<string, string>;
  /** Whole-context or transport problem. */
  message: string | null;
}

export const IDLE_VALIDATION: ContextValidationState = { status: "idle", accepted: null, fieldErrors: {}, message: null };

export interface RequestContextFormProps {
  agentId: string;
  /** Published version, "draft", or null for the latest version. */
  version: ContextVersionRef;
  /** The context JSON schema from GET /agents/{id}/request-context. */
  schema: JsonSchema;
  value: RequestContextValue;
  onChange: (value: RequestContextValue) => void;
  /** Called with every validation state change (live, through the validate endpoint). */
  onValidation?: (state: ContextValidationState) => void;
  /** Delay after the last edit before validating; tests pass 0. */
  debounceMs?: number;
  /** Prefix for element ids, when several forms share a page. */
  idPrefix?: string;
}

type Mode = "form" | "json";

/**
 * The request context of one agent version as a schema-generated form and as JSON. Both modes edit
 * the same value (switching keeps it); every change is checked by the API, and stale answers are
 * dropped. Reusable by the playground's context panel (P0.19).
 */
export function RequestContextForm({
  agentId,
  version,
  schema,
  value,
  onChange,
  onValidation,
  debounceMs = 400,
  idPrefix = "request-context",
}: RequestContextFormProps) {
  const [mode, setMode] = useState<Mode>("form");
  const [raw, setRaw] = useState(() => formatContextJson(value));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [validation, setValidation] = useState<ContextValidationState>(IDLE_VALIDATION);
  // The last value this component emitted from JSON mode; any other new value came from outside
  // (form edits, a loaded preset) and replaces the JSON text.
  const emittedFromJson = useRef<RequestContextValue | null>(null);
  const onValidationRef = useRef(onValidation);
  onValidationRef.current = onValidation;

  useEffect(() => {
    if (value === emittedFromJson.current) return;
    setRaw(formatContextJson(value));
    setJsonError(null);
  }, [value]);

  // Form mode drops "" (an empty input means "not supplied"); JSON mode keeps an explicit "".
  const normalized = useMemo(() => normalizeContext(value, { keepEmptyStrings: mode === "json" }), [value, mode]);
  const valueKey = useMemo(() => JSON.stringify(normalized), [normalized]);

  const publish = (state: ContextValidationState) => {
    setValidation(state);
    onValidationRef.current?.(state);
  };

  useEffect(() => {
    if (jsonError) {
      publish({ status: "invalid", accepted: null, fieldErrors: {}, message: jsonError });
      return;
    }
    const controller = new AbortController();
    let current = true;
    publish({ status: "checking", accepted: null, fieldErrors: {}, message: null });
    const timer = setTimeout(() => {
      validateRequestContext(agentId, JSON.parse(valueKey) as RequestContextValue, version, controller.signal)
        .then((accepted) => {
          if (current) publish({ status: "valid", accepted, fieldErrors: {}, message: null });
        })
        .catch((reason: unknown) => {
          if (!current) return;
          if (reason instanceof RequestContextValidationError) {
            publish({ status: "invalid", accepted: null, fieldErrors: reason.fieldErrors, message: reason.banner });
          } else {
            publish({ status: "error", accepted: null, fieldErrors: {}, message: reason instanceof Error ? reason.message : "Validation failed" });
          }
        });
    }, debounceMs);
    return () => {
      current = false;
      clearTimeout(timer);
      controller.abort();
    };
    // publish is stable in effect (it only uses setters and a ref).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, version, valueKey, jsonError, debounceMs]);

  const setJson = (text: string) => {
    setRaw(text);
    const parsed = parseContextJson(text);
    if ("error" in parsed) {
      setJsonError(parsed.error);
      return;
    }
    setJsonError(null);
    emittedFromJson.current = parsed.value;
    onChange(parsed.value);
  };

  const properties = schema.properties || {};
  const hasFields = Object.keys(properties).length > 0;
  const fieldErrorEntries = Object.entries(validation.fieldErrors);
  // Keys the selected version does not declare (e.g. from a context saved for another version):
  // listed in form mode with their error and a Remove action, since the form has no input for them.
  const undeclared = Array.from(new Set([
    ...Object.keys(value).filter((key) => !(key in properties)),
    ...Object.keys(validation.fieldErrors).filter((key) => !(key in properties)),
  ]));
  const declaredErrors = Object.fromEntries(fieldErrorEntries.filter(([key]) => key in properties));
  const removeKey = (key: string) => {
    const next = { ...value };
    delete next[key];
    onChange(next);
  };

  return (
    <div className="space-y-4" data-testid="request-context-form">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="group" aria-label="Context input mode" className="flex rounded-md border border-border p-1">
          {(["form", "json"] as const).map((item) => {
            const blocked = item === "form" && mode === "json" && Boolean(jsonError);
            return (
              <button
                key={item}
                type="button"
                aria-pressed={mode === item}
                disabled={blocked}
                title={blocked ? "Fix the JSON first" : undefined}
                onClick={() => setMode(item)}
                className={`rounded px-3 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50 ${mode === item ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent"}`}
              >
                {item === "form" ? "Form" : "JSON"}
              </button>
            );
          })}
        </div>
        <ValidationBadge state={validation} />
      </div>

      {mode === "form" ? (
        <>
          {hasFields ? (
            <JsonSchemaForm schema={schema} value={value} onChange={onChange} errors={declaredErrors} />
          ) : (
            <p className="text-sm text-muted-foreground">This version declares no context fields.</p>
          )}
          {undeclared.length > 0 ? (
            <div data-testid="context-undeclared-keys" className="space-y-2 rounded-md border border-destructive/30 bg-destructive/10 p-3">
              <p className="text-sm text-destructive-text">Not declared by this version:</p>
              <ul className="space-y-1">
                {undeclared.map((key) => (
                  <li key={key} className="flex flex-wrap items-center gap-2 text-xs text-destructive-text">
                    <span className="font-mono">{key}</span>
                    {validation.fieldErrors[key] ? <span role="alert">{validation.fieldErrors[key]}</span> : null}
                    {key in value ? (
                      <button type="button" aria-label={`Remove ${key}`} onClick={() => removeKey(key)} className="ml-auto rounded border border-border bg-background px-2 py-0.5 text-foreground hover:bg-accent">
                        Remove
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      ) : (
        <div className="space-y-1">
          <label htmlFor={`${idPrefix}-json`} className="sr-only">Request context JSON</label>
          <textarea
            id={`${idPrefix}-json`}
            rows={10}
            spellCheck={false}
            value={raw}
            aria-invalid={Boolean(jsonError) || fieldErrorEntries.length > 0}
            onChange={(event) => setJson(event.target.value)}
            className="w-full rounded-md border border-input bg-background p-3 font-mono text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {jsonError ? <p role="alert" data-testid="context-json-error" className="text-xs text-destructive-text">{jsonError}</p> : null}
          {fieldErrorEntries.length > 0 ? (
            <ul role="alert" data-testid="context-json-field-errors" className="space-y-1 text-xs text-destructive-text">
              {fieldErrorEntries.map(([key, message]) => <li key={key}><span className="font-mono">{key}</span>: {message}</li>)}
            </ul>
          ) : null}
        </div>
      )}

      {validation.message && !jsonError ? (
        <p role="alert" data-testid="context-validation-message" className={`rounded-md border p-3 text-sm ${validation.status === "error" ? "border-border bg-muted/40 text-foreground" : "border-destructive/30 bg-destructive/10 text-destructive-text"}`}>
          {validation.status === "error" ? `Could not validate: ${validation.message}` : validation.message}
        </p>
      ) : null}
    </div>
  );
}

function ValidationBadge({ state }: { state: ContextValidationState }) {
  const problems = Object.keys(state.fieldErrors).length + (state.message ? 1 : 0);
  const text = {
    idle: "Not checked",
    checking: "Checking…",
    valid: "Valid",
    invalid: problems === 1 ? "1 problem" : `${problems} problems`,
    error: "Not checked",
  }[state.status];
  const tone = state.status === "valid"
    ? "bg-primary/10 text-primary"
    : state.status === "invalid"
      ? "bg-destructive/10 text-destructive-text"
      : "bg-muted text-muted-foreground";
  return (
    <span role="status" aria-live="polite" data-testid="context-validation-status" data-state={state.status} className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${tone}`}>
      {text}
    </span>
  );
}
