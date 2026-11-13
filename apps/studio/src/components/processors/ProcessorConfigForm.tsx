"use client";

import React, { useState } from "react";
import type { ProcessorConfigSchema } from "@/lib/processors";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function labelFor(name: string): string {
  return name.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

/** A list of free strings, one per line. Keeps the raw text so blank lines can be typed. */
function LinesField({ id, value, onChange }: { id: string; value: unknown; onChange: (value: string[]) => void }) {
  const [raw, setRaw] = useState(Array.isArray(value) ? value.join("\n") : "");
  return (
    <textarea
      id={id}
      rows={3}
      className={`${inputClass} font-mono`}
      placeholder="One per line"
      value={raw}
      onChange={(event) => {
        setRaw(event.target.value);
        onChange(event.target.value.split("\n").filter((line) => line.length > 0));
      }}
    />
  );
}

/**
 * The config form for one processor, from the catalog's config schema. Covers the shapes the
 * built-in types use: enum (select), list of enum (checkboxes), list of strings (lines) and
 * integers. The server validates the config again.
 */
export function ProcessorConfigForm({ schema, value, onChange, idPrefix }: {
  schema: ProcessorConfigSchema;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
  idPrefix: string;
}) {
  const properties = Object.entries(schema.properties || {});
  if (properties.length === 0) return <p className="text-sm text-muted-foreground">No configuration.</p>;
  const set = (name: string, next: unknown) => onChange({ ...value, [name]: next });

  return (
    <div className="space-y-3">
      {properties.map(([name, property]) => {
        const id = `${idPrefix}-${name}`;
        const required = (schema.required || []).includes(name);
        const label = `${labelFor(name)}${required ? " *" : ""}`;
        const current = value[name];
        let control: React.ReactNode;

        if (property.type === "array" && property.items?.enum) {
          const selected = Array.isArray(current) ? current.map(String) : [];
          return (
            <fieldset key={name} className="space-y-1">
              <legend className="text-sm font-medium text-foreground">{label}</legend>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {property.items.enum.map((option) => {
                  const optionValue = String(option);
                  return (
                    <label key={optionValue} className="flex items-center gap-1.5 text-sm text-foreground">
                      <input
                        type="checkbox"
                        className="h-4 w-4 rounded border-input"
                        checked={selected.includes(optionValue)}
                        onChange={(event) => set(
                          name,
                          event.target.checked
                            ? property.items!.enum!.map(String).filter((o) => o === optionValue || selected.includes(o))
                            : selected.filter((o) => o !== optionValue),
                        )}
                      />
                      <span className="font-mono text-xs">{optionValue}</span>
                    </label>
                  );
                })}
              </div>
              {property.description ? <p className="text-xs text-muted-foreground">{property.description}</p> : null}
            </fieldset>
          );
        }

        if (property.enum) {
          control = (
            <select id={id} className={inputClass} value={current == null ? "" : String(current)} onChange={(event) => set(name, event.target.value)}>
              {property.enum.map((option) => <option key={String(option)} value={String(option)}>{String(option)}</option>)}
            </select>
          );
        } else if (property.type === "array") {
          control = <LinesField id={id} value={current} onChange={(next) => set(name, next)} />;
        } else if (property.type === "integer" || property.type === "number") {
          control = (
            <input
              id={id}
              type="number"
              step={property.type === "integer" ? 1 : "any"}
              min={property.minimum}
              max={property.maximum}
              className={inputClass}
              value={current == null ? "" : String(current)}
              onChange={(event) => {
                const raw = event.target.value;
                if (raw === "") {
                  const next = { ...value };
                  delete next[name];
                  onChange(next);
                } else {
                  set(name, Number(raw));
                }
              }}
            />
          );
        } else {
          control = <input id={id} type="text" className={inputClass} value={current == null ? "" : String(current)} onChange={(event) => set(name, event.target.value)} />;
        }

        return (
          <div key={name} className="space-y-1">
            <label htmlFor={id} className="block text-sm font-medium text-foreground">{label}</label>
            {control}
            {property.description ? <p className="text-xs text-muted-foreground">{property.description}</p> : null}
          </div>
        );
      })}
    </div>
  );
}
