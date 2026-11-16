"use client";

import React, { useState } from "react";
import { ModelPicker } from "@/components/shared/ModelPicker";
import { JsonSchemaForm } from "@/components/tools/JsonSchemaForm";
import { PROCESSOR_PHASE_LABELS, PROCESSOR_PHASES } from "@/lib/processors";
import type { JsonSchema } from "@/lib/tools";
import {
  DEFAULT_MAX_PROCESSORS_PER_PHASE,
  defaultProcessorConfig,
  newClientId,
  type CatalogSchema,
  type EditableProcessor,
  type ModelOption,
  type ProcessorCatalog,
  type ProcessorDeclaration,
  type ProcessorPhase,
} from "./specSections";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const smallButton = "rounded-md border border-border px-2 py-1 text-xs text-card-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";

function labelFor(name: string, schema: CatalogSchema): string {
  return schema.title || name.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

/** A multi-select over a fixed set (PII kinds, moderation categories). */
function EnumChecklist({ id, label, description, options, value, onChange }: {
  id: string;
  label: string;
  description?: string;
  options: string[];
  value: string[];
  onChange: (value: string[]) => void;
}) {
  return (
    <fieldset className="space-y-1" aria-describedby={value.length === 0 ? `${id}-problem` : undefined} aria-invalid={value.length === 0}>
      <legend className="text-sm font-medium text-foreground">{label}</legend>
      <div className="flex flex-wrap gap-2">
        {options.map((option) => (
          <label key={option} className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-card-foreground">
            <input
              type="checkbox"
              id={`${id}-${option}`}
              checked={value.includes(option)}
              onChange={() => onChange(value.includes(option) ? value.filter((v) => v !== option) : options.filter((o) => o === option || value.includes(o)))}
            />
            {option}
          </label>
        ))}
      </div>
      {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
      <div id={`${id}-problem`} aria-live="polite">
        {value.length === 0 ? <p className="text-xs text-destructive-text">Pick at least one</p> : null}
      </div>
    </fieldset>
  );
}

/** One item per line; blank lines are kept while typing and dropped on save. */
function LinesInput({ id, label, description, value, maxItems, maxLength, onChange }: {
  id: string;
  label: string;
  description?: string;
  value: string[];
  maxItems?: number;
  maxLength?: number;
  onChange: (value: string[]) => void;
}) {
  const items = value.filter((v) => v.length > 0);
  const problems: string[] = [];
  if (items.length === 0) problems.push(`${label} needs at least one entry`);
  if (maxItems !== undefined && items.length > maxItems) problems.push(`At most ${maxItems} entries`);
  if (maxLength !== undefined && items.some((v) => v.length > maxLength)) problems.push(`Each entry may have at most ${maxLength} characters`);
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-foreground">{label} *</label>
      <textarea
        id={id}
        rows={3}
        className={`${inputClass} font-mono`}
        placeholder="One per line"
        aria-invalid={problems.length > 0}
        aria-describedby={problems.length > 0 ? `${id}-problems` : undefined}
        value={value.join("\n")}
        onChange={(e) => onChange(e.target.value.split("\n"))}
      />
      {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
      <div id={`${id}-problems`} aria-live="polite">
        {problems.map((p) => <p key={p} className="text-xs text-destructive-text">{p}</p>)}
      </div>
    </div>
  );
}

/** The moderation model: a provider and model from the allowlist (the shared picker), or free text where the list is unavailable. */
export function ModerationModelPicker({ id, value, models, modelsError, onChange }: {
  id: string;
  value: { provider?: string; name?: string } | undefined;
  models: ModelOption[] | null;
  modelsError: string | null;
  onChange: (value: { provider: string; name: string }) => void;
}) {
  const provider = value?.provider ?? "";
  const name = value?.name ?? "";
  return (
    <ModelPicker
      providerId={`${id}-provider`}
      modelId={id}
      providerLabel="Moderation provider *"
      modelLabel="Moderation model *"
      provider={provider}
      name={name}
      models={models}
      loadError={modelsError}
      problem={provider && !name ? "Type or choose a model name" : null}
      hint="Must be in the platform allowlist; checked at publish and at run time."
      onChange={onChange}
    />
  );
}

/** Config form for one processor: generated from the catalog schema, special controls where JsonSchemaForm does not fit. */
export function ProcessorConfigForm({ idPrefix, declaration, config, models, modelsError, onChange }: {
  idPrefix: string;
  declaration: ProcessorDeclaration;
  config: Record<string, unknown>;
  models: ModelOption[] | null;
  modelsError: string | null;
  onChange: (config: Record<string, unknown>) => void;
}) {
  const schema = declaration.config_schema;
  const special: React.ReactNode[] = [];
  const rest: Record<string, CatalogSchema> = {};
  for (const [key, prop] of Object.entries(schema.properties ?? {})) {
    const id = `${idPrefix}-${key}`;
    if (key === "model" && prop.type === "object") {
      special.push(
        <ModerationModelPicker
          key={key}
          id={id}
          value={config.model as { provider?: string; name?: string } | undefined}
          models={models}
          modelsError={modelsError}
          onChange={(model) => onChange({ ...config, model })}
        />,
      );
    } else if (prop.type === "array" && prop.items?.enum) {
      special.push(
        <EnumChecklist
          key={key}
          id={id}
          label={labelFor(key, prop)}
          description={prop.description}
          options={prop.items.enum.map(String)}
          value={Array.isArray(config[key]) ? (config[key] as string[]) : []}
          onChange={(next) => onChange({ ...config, [key]: next })}
        />,
      );
    } else if (prop.type === "array") {
      special.push(
        <LinesInput
          key={key}
          id={id}
          label={labelFor(key, prop)}
          description={prop.description}
          maxItems={prop.maxItems}
          maxLength={prop.items?.maxLength}
          value={Array.isArray(config[key]) ? (config[key] as string[]) : []}
          onChange={(next) => onChange({ ...config, [key]: next })}
        />,
      );
    } else {
      rest[key] = prop;
    }
  }
  const restSchema: JsonSchema = {
    type: "object",
    properties: rest,
    required: (schema.required ?? []).filter((key) => key in rest),
  };
  const restKeys = Object.keys(rest);
  const restValue = Object.fromEntries(Object.entries(config).filter(([key]) => restKeys.includes(key)));
  return (
    <div className="space-y-3">
      {special}
      {restKeys.length > 0 ? (
        <JsonSchemaForm
          idPrefix={idPrefix}
          schema={restSchema}
          value={restValue}
          onChange={(next) => onChange({ ...config, ...next })}
        />
      ) : null}
    </div>
  );
}

const PHASE_NOTES: Record<ProcessorPhase, string | null> = {
  input: null,
  step: "Checks each tool call's arguments before the tool runs and its result before the model sees it. A block replaces the result with a message and the run continues.",
  stream:
    "Live streaming stays on: each chunk is filtered as it streams, and the finished answer is checked again so the stored answer matches what was shown. A pattern split across two chunks is caught only by the second check.",
  output: "Without a stream processor, any output processor holds the answer until it is checked.",
};

interface PhaseListProps {
  phase: ProcessorPhase;
  value: EditableProcessor[];
  onChange: (value: EditableProcessor[]) => void;
  catalog: ProcessorCatalog | null;
  models: ModelOption[] | null;
  modelsError: string | null;
  /** API messages for single processors, keyed by `clientId`. */
  errors: Record<string, string>;
}

function PhaseList({ phase, value, onChange, catalog, models, modelsError, errors }: PhaseListProps) {
  const [adding, setAdding] = useState("");
  const max = catalog?.limits.max_processors_per_phase ?? DEFAULT_MAX_PROCESSORS_PER_PHASE;
  const available = (catalog?.items ?? []).filter((item) => item.phases.includes(phase));
  const label = PROCESSOR_PHASE_LABELS[phase];
  const title = `${label} processors`;

  const move = (index: number, delta: number) => {
    const next = [...value];
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    onChange(next);
  };

  const add = () => {
    const declaration = available.find((item) => item.id === adding);
    if (!declaration || value.length >= max) return;
    onChange([...value, { type: declaration.id, config: defaultProcessorConfig(declaration, models ?? []), clientId: newClientId() }]);
    setAdding("");
  };

  return (
    <section aria-label={title} data-testid={`processors-${phase}`} className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-medium text-card-foreground">{title}</h2>
        <span className="text-xs text-muted-foreground">{value.length} of {max}, run in order</span>
      </div>
      {PHASE_NOTES[phase] ? <p className="text-xs text-muted-foreground">{PHASE_NOTES[phase]}</p> : null}
      {value.length === 0 ? <p className="text-xs text-muted-foreground">None.</p> : null}
      <ol className="space-y-3">
        {value.map((processor, index) => {
          const declaration = catalog?.items.find((item) => item.id === processor.type);
          const label = declaration?.title ?? processor.type;
          const idPrefix = `processor-${phase}-${index}`;
          return (
            <li key={processor.clientId} data-testid={idPrefix} className="space-y-3 rounded-md border border-border p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-card-foreground">{index + 1}. {label}</span>
                <div className="flex gap-1">
                  <button type="button" className={smallButton} aria-label={`Move ${label} up`} disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
                  <button type="button" className={smallButton} aria-label={`Move ${label} down`} disabled={index === value.length - 1} onClick={() => move(index, 1)}>↓</button>
                  <button type="button" className={smallButton} aria-label={`Remove ${label}`} onClick={() => onChange(value.filter((_, i) => i !== index))}>Remove</button>
                </div>
              </div>
              {declaration ? (
                <>
                  <p className="text-xs text-muted-foreground">{declaration.description}</p>
                  <ProcessorConfigForm
                    idPrefix={idPrefix}
                    declaration={declaration}
                    config={processor.config}
                    models={models}
                    modelsError={modelsError}
                    onChange={(config) => onChange(value.map((item, i) => (i === index ? { ...item, config } : item)))}
                  />
                </>
              ) : (
                <pre className="overflow-x-auto rounded bg-muted p-2 text-xs text-muted-foreground">{JSON.stringify(processor.config, null, 2)}</pre>
              )}
              {errors[processor.clientId] ? <p role="alert" data-testid={`${idPrefix}-error`} className="text-xs text-destructive-text">{errors[processor.clientId]}</p> : null}
            </li>
          );
        })}
      </ol>
      <div className="flex gap-2">
        <select aria-label={`${label} processor type`} className={inputClass} value={adding} onChange={(e) => setAdding(e.target.value)} disabled={!catalog || value.length >= max}>
          <option value="">Choose a processor type…</option>
          {available.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
        </select>
        <button type="button" className={smallButton} aria-label={`Add ${phase} processor`} onClick={add} disabled={!adding || value.length >= max}>Add</button>
      </div>
    </section>
  );
}

interface ProcessorsEditorProps {
  inputProcessors: EditableProcessor[];
  stepProcessors: EditableProcessor[];
  streamProcessors: EditableProcessor[];
  outputProcessors: EditableProcessor[];
  onInputChange: (value: EditableProcessor[]) => void;
  onStepChange: (value: EditableProcessor[]) => void;
  onStreamChange: (value: EditableProcessor[]) => void;
  onOutputChange: (value: EditableProcessor[]) => void;
  catalog: ProcessorCatalog | null;
  catalogError: string | null;
  models: ModelOption[] | null;
  modelsError: string | null;
  /** An API message for the whole section (e.g. a disallowed moderation model). */
  sectionError?: string | null;
  /** API messages for single processors, keyed by `clientId`. */
  inputErrors?: Record<string, string>;
  stepErrors?: Record<string, string>;
  streamErrors?: Record<string, string>;
  outputErrors?: Record<string, string>;
}

const PHASE_CHANGE = { input: "onInputChange", step: "onStepChange", stream: "onStreamChange", output: "onOutputChange" } as const;

/** Processors (guardrails) section of the agent editor. */
export function ProcessorsEditor(props: ProcessorsEditorProps) {
  return (
    <fieldset className="space-y-4 rounded-md border border-border p-4">
      <legend className="px-1 text-sm font-medium text-card-foreground">Processors</legend>
      <p className="text-xs text-muted-foreground">
        Guardrails on the user&apos;s input, on tool calls and their results, on the streamed answer and on the model&apos;s output.
        Blocked input or output fails the turn with a fixed message. Request context values are not processed; moderation checks only the final answer.
      </p>
      {props.catalogError ? <p role="alert" className="text-xs text-destructive-text">Could not load the processor catalog: {props.catalogError}</p> : null}
      {props.sectionError ? <p role="alert" data-testid="processors-error" className="text-xs text-destructive-text">{props.sectionError}</p> : null}
      {PROCESSOR_PHASES.map((phase) => (
        <PhaseList
          key={phase}
          phase={phase}
          value={props[`${phase}Processors`]}
          onChange={props[PHASE_CHANGE[phase]]}
          catalog={props.catalog}
          models={props.models}
          modelsError={props.modelsError}
          errors={props[`${phase}Errors`] ?? {}}
        />
      ))}
    </fieldset>
  );
}
