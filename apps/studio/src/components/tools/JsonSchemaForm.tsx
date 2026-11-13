"use client";

import React, { useState } from "react";
import type { JsonSchema } from "@/lib/tools";

type JsonObject = Record<string, unknown>;

interface JsonSchemaFormProps {
  schema: JsonSchema;
  value: JsonObject;
  onChange: (value: JsonObject) => void;
  /** Server-side messages keyed by dotted input path ("options.retries"), e.g. from a 422. */
  errors?: Record<string, string>;
  /** Prefix of the generated element ids (default "schema"); set it when several forms share a page. */
  idPrefix?: string;
}

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function labelFor(name: string, schema: JsonSchema): string {
  return schema.title || name.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function Field({ name, schema, value, required, onChange, path, fieldPath, errors, idPrefix }: {
  name: string;
  schema: JsonSchema;
  value: unknown;
  required: boolean;
  onChange: (value: unknown) => void;
  path: string;
  fieldPath: string[];
  errors?: Record<string, string>;
  idPrefix: string;
}) {
  const [touched, setTouched] = useState(false);
  const label = labelFor(name, schema);
  const id = `${idPrefix}-${path}`;
  const empty = value === undefined || value === null || value === "";
  let error = touched && required && empty ? `${label} is required` : "";
  if (touched && !empty && schema.type === "integer" && !Number.isInteger(value)) error = `${label} must be an integer`;
  if (touched && !empty && schema.type === "number" && typeof value !== "number") error = `${label} must be a number`;
  const dotted = fieldPath.join(".");
  // Leaf controls (arrays included) also own errors below them, e.g. "tags.0".
  const serverError = errors?.[dotted] ?? (schema.type === "object" ? undefined
    : Object.entries(errors || {}).find(([key]) => key.startsWith(`${dotted}.`))?.[1]);
  if (serverError) error = serverError;

  if (schema.type === "object") {
    const objectValue = value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
    return (
      <fieldset className="space-y-3 rounded-md border border-border p-4">
        <legend className="px-1 text-sm font-medium text-foreground">{label}{required ? " *" : ""}</legend>
        {error ? <p role="alert" data-testid={`field-error-${fieldPath.join(".")}`} className="text-xs text-destructive">{error}</p> : null}
        {Object.entries(schema.properties || {}).map(([childName, childSchema]) => (
          <Field
            key={childName}
            name={childName}
            schema={childSchema}
            value={objectValue[childName]}
            required={(schema.required || []).includes(childName)}
            path={`${path}-${childName}`}
            fieldPath={[...fieldPath, childName]}
            errors={errors}
            idPrefix={idPrefix}
            onChange={(next) => onChange({ ...objectValue, [childName]: next })}
          />
        ))}
      </fieldset>
    );
  }

  const labelNode = <label htmlFor={id} className="block text-sm font-medium text-foreground">{label}{required ? " *" : ""}</label>;
  let control;
  if (schema.enum) {
    control = (
      <select id={id} aria-invalid={Boolean(error)} className={inputClass} value={value == null ? "" : String(value)} onBlur={() => setTouched(true)} onChange={(event) => onChange(event.target.value)}>
        <option value="">Select…</option>
        {schema.enum.map((option) => <option key={String(option)} value={String(option)}>{String(option)}</option>)}
      </select>
    );
  } else if (schema.type === "boolean") {
    control = <input id={id} type="checkbox" aria-invalid={Boolean(error)} checked={Boolean(value)} onBlur={() => setTouched(true)} onChange={(event) => onChange(event.target.checked)} className="h-4 w-4 rounded border-input" />;
  } else if (schema.type === "array") {
    const display = Array.isArray(value) ? value.join("\n") : "";
    control = <textarea id={id} aria-invalid={Boolean(error)} className={inputClass} placeholder="One item per line" value={display} onBlur={() => setTouched(true)} onChange={(event) => {
      const lines = event.target.value.split("\n").filter((item) => item.length > 0);
      const parsed = lines.map((item) => schema.items?.type === "number" || schema.items?.type === "integer" ? Number(item) : schema.items?.type === "boolean" ? item === "true" : item);
      onChange(parsed);
    }} />;
  } else {
    const numeric = schema.type === "number" || schema.type === "integer";
    control = <input id={id} aria-invalid={Boolean(error)} type={numeric ? "number" : "text"} step={schema.type === "integer" ? 1 : numeric ? "any" : undefined} className={inputClass} value={value == null ? "" : String(value)} onBlur={() => setTouched(true)} onChange={(event) => {
      const raw = event.target.value;
      if (!numeric || raw === "") onChange(raw);
      else {
        const number = Number(raw);
        onChange(Number.isNaN(number) ? raw : number);
      }
    }} />;
  }

  return (
    <div className="space-y-1">
      {labelNode}
      {control}
      {schema.description ? <p className="text-xs text-muted-foreground">{schema.description}</p> : null}
      {error ? <p role="alert" data-testid={`field-error-${fieldPath.join(".")}`} className="text-xs text-destructive">{error}</p> : null}
    </div>
  );
}

export function JsonSchemaForm({ schema, value, onChange, errors, idPrefix = "schema" }: JsonSchemaFormProps) {
  // Errors for paths the form has no top-level field for (e.g. an unexpected key from JSON mode).
  const properties = schema.properties || {};
  const unmatched = Object.entries(errors || {}).filter(([key]) => !(key.split(".")[0] in properties));
  return (
    <div className="space-y-4">
      {Object.entries(schema.properties || {}).map(([name, property]) => (
        <Field
          key={name}
          name={name}
          schema={property}
          value={value[name]}
          required={(schema.required || []).includes(name)}
          path={name}
          fieldPath={[name]}
          errors={errors}
          idPrefix={idPrefix}
          onChange={(next) => onChange({ ...value, [name]: next })}
        />
      ))}
      {Object.keys(schema.properties || {}).length === 0 ? <p className="text-sm text-muted-foreground">This tool takes no input.</p> : null}
      {unmatched.length > 0 ? (
        <ul role="alert" data-testid="field-errors-unmatched" className="space-y-1 text-xs text-destructive">
          {unmatched.map(([key, message]) => <li key={key}><span className="font-mono">{key}</span>: {message}</li>)}
        </ul>
      ) : null}
    </div>
  );
}
