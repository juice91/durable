"use client";

/**
 * The Output step's field builder (AC.13): a table of fields (name, type, required, can be null,
 * description, allowed values for strings, item type for lists, nested fields for objects) that
 * writes the JSON Schema subset. Keywords the builder has no column for (`minimum`, `format`,
 * `title`...) are kept as they are, so a schema round-trips through it unchanged. A schema it cannot
 * show (see `builderFromSchema`) is edited in the raw tab. Tokens only, no fixed widths.
 */
import React, { useEffect, useRef, useState } from "react";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import {
  BUILDER_TYPES,
  builderFieldProblems,
  builderFromSchema,
  newBuilderField,
  OUTPUT_SCHEMA_MAX_DEPTH,
  schemaFromBuilder,
  withBuilderType,
  type BuilderField,
  type BuilderItemType,
  type BuilderRoot,
  type BuilderType,
} from "../agentCapabilities";
import { canonicalJson } from "../specSections";
import { fieldClass, labelClass } from "./fieldStyles";
import { hintClass, problemClass } from "./knowledgeMemoryFields";
import { Select } from "@/components/ui/Select";

const smallField = `${fieldClass} py-1.5`;
const ITEM_TYPES = BUILDER_TYPES.filter((t): t is BuilderItemType => t !== "array");

/** Element id of a builder row's name input. */
export const builderNameId = (key: string) => `output-field-${key}-name`;

/** "a, b , c" -> ["a", "b", "c"]; blank text -> null (any string). */
function parseValues(text: string): string[] | null {
  if (text.trim() === "") return null;
  return text.split(",").map((v) => v.trim()).filter((v) => v.length > 0);
}

/** The allowed-values input: keeps the text as typed (a trailing comma) and reports the list. */
function ValuesInput({ id, values, onChange, label }: { id: string; values: string[] | null; onChange: (values: string[] | null) => void; label: string }) {
  const [text, setText] = useState(() => (values ?? []).join(", "));
  const emitted = useRef(values);
  useEffect(() => {
    if (values !== emitted.current) {
      emitted.current = values;
      setText((values ?? []).join(", "));
    }
  }, [values]);
  return (
    <input
      id={id}
      type="text"
      aria-label={label}
      placeholder="Any text (or list allowed values: a, b, c)"
      value={text}
      onChange={(e) => {
        const next = parseValues(e.target.value);
        setText(e.target.value);
        emitted.current = next;
        onChange(next);
      }}
      className={smallField}
    />
  );
}

function FieldRows({
  fields,
  onChange,
  depth,
  parentLabel,
}: {
  fields: BuilderField[];
  onChange: (fields: BuilderField[]) => void;
  /** The object depth of these fields' parent (the root is 1). */
  depth: number;
  parentLabel: string;
}) {
  const update = (index: number, field: BuilderField) => onChange(fields.map((f, i) => (i === index ? field : f)));
  const remove = (index: number) => onChange(fields.filter((_, i) => i !== index));
  return (
    <ul className="min-w-0 space-y-3" aria-label={parentLabel}>
      {fields.map((field, index) => {
        const label = field.name || `field ${index + 1}`;
        const problems = builderFieldProblems(field, fields);
        const nested = field.type === "object" || (field.type === "array" && field.itemType === "object");
        const problemId = `output-field-${field.key}-problem`;
        return (
          <li key={field.key} data-testid="output-field-row" data-field-name={field.name} className="min-w-0 space-y-2 rounded-md border border-border p-2">
            <div className="flex min-w-0 flex-wrap items-end gap-2">
              <div className="min-w-0 flex-[2_1_10rem]">
                <label htmlFor={builderNameId(field.key)} className={labelClass}>
                  Name
                </label>
                <input
                  id={builderNameId(field.key)}
                  type="text"
                  value={field.name}
                  onChange={(e) => update(index, { ...field, name: e.target.value })}
                  aria-invalid={problems.length > 0 ? true : undefined}
                  aria-describedby={problemId}
                  className={`${smallField} font-mono`}
                />
              </div>
              <div className="min-w-0 flex-[1_1_7rem]">
                <label htmlFor={`output-field-${field.key}-type`} className={labelClass}>
                  Type
                </label>
                <Select
                  id={`output-field-${field.key}-type`}
                  value={field.type}
                  onValueChange={(v) => update(index, withBuilderType(field, v as BuilderType))}
                  className={smallField}
                >
                  {BUILDER_TYPES.map((type) => (
                    <option key={type} value={type}>
                      {type === "array" ? "list" : type}
                    </option>
                  ))}
                </Select>
              </div>
              {field.type === "array" ? (
                <div className="min-w-0 flex-[1_1_7rem]">
                  <label htmlFor={`output-field-${field.key}-items`} className={labelClass}>
                    List of
                  </label>
                  <Select
                    id={`output-field-${field.key}-items`}
                    value={field.itemType}
                    onValueChange={(v) => update(index, { ...field, itemType: v as BuilderItemType, itemRest: {} })}
                    className={smallField}
                  >
                    {ITEM_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {type}
                      </option>
                    ))}
                  </Select>
                </div>
              ) : null}
              <IconButton label={`Remove ${label}`} icon={<X aria-hidden="true" />} tooltip={false} onClick={() => remove(index)} />
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
              <label className="inline-flex items-center gap-2 text-label text-foreground">
                <input type="checkbox" aria-label={`Required: ${label}`} className="size-4 accent-primary" checked={field.required} onChange={(e) => update(index, { ...field, required: e.target.checked })} />
                Required
              </label>
              <label className="inline-flex items-center gap-2 text-label text-foreground">
                <input type="checkbox" aria-label={`Can be null: ${label}`} className="size-4 accent-primary" checked={field.nullable} onChange={(e) => update(index, { ...field, nullable: e.target.checked })} />
                Can be null
              </label>
            </div>
            <input
              type="text"
              aria-label={`Description of ${label}`}
              placeholder="Description (helps the model fill it in)"
              value={field.description}
              onChange={(e) => update(index, { ...field, description: e.target.value })}
              className={smallField}
            />
            {field.type === "string" ? (
              <ValuesInput
                id={`output-field-${field.key}-values`}
                label={`Allowed values of ${label}`}
                values={field.enumValues}
                onChange={(enumValues) => update(index, { ...field, enumValues })}
              />
            ) : null}
            <p id={problemId} aria-live="polite" className={problemClass}>
              {problems.join(". ")}
            </p>
            {nested ? (
              <div className="min-w-0 space-y-2 border-l-2 border-border pl-3">
                <p className="text-caption text-muted-foreground">{field.type === "object" ? `Fields of ${label}` : `Fields of each ${label} item`}</p>
                <FieldRows
                  fields={field.children}
                  onChange={(children) => update(index, { ...field, children })}
                  depth={depth + 1}
                  parentLabel={`Fields of ${label}`}
                />
                <Button
                  size="sm"
                  disabled={depth + 1 > OUTPUT_SCHEMA_MAX_DEPTH}
                  onClick={() => update(index, { ...field, children: [...field.children, newBuilderField()] })}
                >
                  <Plus aria-hidden="true" />
                  Add field to {label}
                </Button>
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The builder for `schema`. It keeps its rows while the schema it last wrote comes back, and rebuilds
 * them when the schema changes from outside (the raw tab, "Add citations field").
 */
export function OutputSchemaBuilder({ schema, onChange }: { schema: unknown; onChange: (schema: Record<string, unknown>) => void }) {
  const [root, setRoot] = useState<BuilderRoot | null>(() => builderFromSchema(schema));
  const emitted = useRef(canonicalJson(schema));
  useEffect(() => {
    const incoming = canonicalJson(schema);
    if (incoming !== emitted.current) {
      emitted.current = incoming;
      setRoot(builderFromSchema(schema));
    }
  }, [schema]);

  if (!root) {
    return (
      <p data-testid="output-builder-unavailable" className="rounded-md border border-border bg-muted p-3 text-label text-muted-foreground">
        The field builder cannot show this schema (it is not valid JSON, or uses a shape the builder has no row for). Edit it in the JSON
        Schema tab.
      </p>
    );
  }

  const write = (next: BuilderRoot) => {
    setRoot(next);
    const out = schemaFromBuilder(next);
    emitted.current = canonicalJson(out);
    onChange(out);
  };

  return (
    <div data-testid="output-builder" className="min-w-0 space-y-3">
      {root.fields.length === 0 ? <p className={hintClass}>No fields yet: the answer would be an empty object.</p> : null}
      <FieldRows fields={root.fields} onChange={(fields) => write({ ...root, fields })} depth={1} parentLabel="Output fields" />
      <Button size="sm" onClick={() => write({ ...root, fields: [...root.fields, newBuilderField()] })}>
        <Plus aria-hidden="true" />
        Add field
      </Button>
    </div>
  );
}
