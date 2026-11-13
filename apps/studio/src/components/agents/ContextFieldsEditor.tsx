"use client";

import React from "react";
import {
  contextFieldErrors,
  MAX_CONTEXT_FIELDS,
  type ContextField,
  type ContextFieldType,
} from "./specSections";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const smallButton = "rounded-md border border-border px-2 py-1 text-xs text-card-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";
const FIELD_TYPES: ContextFieldType[] = ["string", "integer", "number", "boolean", "enum"];

interface ContextFieldsEditorProps {
  value: ContextField[];
  onChange: (value: ContextField[]) => void;
  /** Names used as `{{context.<name>}}` in the instructions (for the "used" hint). */
  usedNames: string[];
  /** Inserts `{{context.<name>}}` into the instructions. */
  onInsertPlaceholder?: (name: string) => void;
  /** An API message about the context fields. */
  sectionError?: string | null;
}

function nextName(fields: ContextField[]): string {
  for (let i = 1; ; i++) {
    const name = `field_${i}`;
    if (!fields.some((f) => f.name === name)) return name;
  }
}

/** Request context section: typed fields a caller supplies per run, usable as instruction placeholders. */
export function ContextFieldsEditor({ value, onChange, usedNames, onInsertPlaceholder, sectionError }: ContextFieldsEditorProps) {
  const update = (index: number, patch: Partial<ContextField>) => {
    onChange(value.map((field, i) => (i === index ? { ...field, ...patch } : field)));
  };

  return (
    <fieldset className="space-y-4 rounded-md border border-border p-4" data-testid="context-fields">
      <legend className="px-1 text-sm font-medium text-card-foreground">Request context</legend>
      <p className="text-xs text-muted-foreground">
        Typed values a caller passes with each run. Use them in the instructions as{" "}
        <code className="font-mono">{"{{context.<name>}}"}</code>; tools read them too.{" "}
        <code className="font-mono">tenant_id</code>, <code className="font-mono">user_id</code> and{" "}
        <code className="font-mono">run_id</code> are reserved.
      </p>
      {sectionError ? <p role="alert" data-testid="context-fields-error" className="text-xs text-destructive-text">{sectionError}</p> : null}
      {value.length === 0 ? <p className="text-xs text-muted-foreground">No context fields.</p> : null}
      <ol className="space-y-3">
        {value.map((field, index) => {
          const prefix = `context-field-${index}`;
          const problems = contextFieldErrors(field, index, value);
          const placeholder = `{{context.${field.name}}}`;
          return (
            <li key={index} data-testid={prefix} className="space-y-3 rounded-md border border-border p-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="space-y-1">
                  <label htmlFor={`${prefix}-name`} className="block text-sm font-medium text-foreground">Field name</label>
                  <input id={`${prefix}-name`} className={`${inputClass} font-mono`} aria-invalid={problems.length > 0} aria-describedby={`${prefix}-problems`} value={field.name} onChange={(e) => update(index, { name: e.target.value })} />
                </div>
                <div className="space-y-1">
                  <label htmlFor={`${prefix}-type`} className="block text-sm font-medium text-foreground">Type</label>
                  <select
                    id={`${prefix}-type`}
                    className={inputClass}
                    value={field.type}
                    onChange={(e) => {
                      const type = e.target.value as ContextFieldType;
                      update(index, {
                        type,
                        max_length: type === "string" ? field.max_length ?? null : null,
                        values: type === "enum" ? field.values ?? [] : [],
                      });
                    }}
                  >
                    {FIELD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
                <label className="inline-flex items-center gap-2 self-end pb-2 text-sm text-card-foreground">
                  <input id={`${prefix}-required`} type="checkbox" checked={Boolean(field.required)} onChange={(e) => update(index, { required: e.target.checked })} />
                  Required
                </label>
              </div>
              <div className="space-y-1">
                <label htmlFor={`${prefix}-description`} className="block text-sm font-medium text-foreground">Description</label>
                <input id={`${prefix}-description`} className={inputClass} maxLength={500} value={field.description ?? ""} onChange={(e) => update(index, { description: e.target.value })} />
              </div>
              {field.type === "string" ? (
                <div className="space-y-1">
                  <label htmlFor={`${prefix}-max-length`} className="block text-sm font-medium text-foreground">Max length (optional)</label>
                  <input
                    id={`${prefix}-max-length`}
                    type="number"
                    min={1}
                    max={8192}
                    step={1}
                    className={inputClass}
                    value={field.max_length ?? ""}
                    onChange={(e) => update(index, { max_length: e.target.value === "" ? null : Number(e.target.value) })}
                  />
                </div>
              ) : null}
              {field.type === "enum" ? (
                <div className="space-y-1">
                  <label htmlFor={`${prefix}-values`} className="block text-sm font-medium text-foreground">Allowed values</label>
                  <textarea
                    id={`${prefix}-values`}
                    aria-describedby={`${prefix}-problems`}
                    rows={3}
                    className={`${inputClass} font-mono`}
                    placeholder="One value per line"
                    value={(field.values ?? []).join("\n")}
                    onChange={(e) => update(index, { values: e.target.value.split("\n") })}
                  />
                </div>
              ) : null}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs text-muted-foreground">
                  <code className="font-mono">{placeholder}</code>
                  {usedNames.includes(field.name) ? " used in the instructions" : " not used in the instructions"}
                </span>
                <div className="flex gap-1">
                  {onInsertPlaceholder ? (
                    <button type="button" className={smallButton} disabled={problems.length > 0} onClick={() => onInsertPlaceholder(field.name)}>
                      Insert placeholder
                    </button>
                  ) : null}
                  <button type="button" className={smallButton} aria-label={`Remove context field ${field.name}`} onClick={() => onChange(value.filter((_, i) => i !== index))}>
                    Remove
                  </button>
                </div>
              </div>
              {/* Polite and persistent: announced when the problems change, not on every keystroke. */}
              <div id={`${prefix}-problems`} aria-live="polite" data-testid={`${prefix}-problems`}>
                {problems.map((problem) => <p key={problem} className="text-xs text-destructive-text">{problem}</p>)}
              </div>
            </li>
          );
        })}
      </ol>
      <button
        type="button"
        className={smallButton}
        disabled={value.length >= MAX_CONTEXT_FIELDS}
        onClick={() => onChange([...value, { name: nextName(value), type: "string", required: false, description: "", max_length: null, values: [] }])}
      >
        Add context field
      </button>
      {value.length >= MAX_CONTEXT_FIELDS ? <p className="text-xs text-muted-foreground">At most {MAX_CONTEXT_FIELDS} fields.</p> : null}
    </fieldset>
  );
}
