"use client";

import React, { useEffect, useRef, useState } from "react";
import { listEmbeddingModels, type EmbeddingModel } from "@/lib/knowledge";
import { fetchModels, fetchProcessorCatalog, type ModelOption } from "../specSections";
import {
  DEFAULT_WORKING_MEMORY_TEMPLATE,
  HISTORY_DEFAULT_LAST_TURNS,
  HISTORY_MAX_LAST_TURNS,
  HISTORY_MAX_TOKENS,
  inheritedRedactKinds,
  isMemoryError,
  KEEP_RECENT_TURNS_RANGE,
  MAX_SCHEMA_BYTES,
  MAX_TEMPLATE_BYTES,
  memoryOf,
  memoryProblems,
  memoryStatus,
  memoryValue,
  MEMORY_SPEC_KEYS,
  piiKindsFromCatalog,
  newObservational,
  newSemanticRecall,
  newWorking,
  OBSERVATIONAL_DEFAULTS,
  OBSERVE_TOKENS_RANGE,
  PII_KINDS,
  RECALL_DEFAULTS,
  RECALL_MESSAGE_RANGE,
  RECALL_TOP_K_RANGE,
  STARTER_WORKING_MEMORY_SCHEMA,
  type MemorySpec,
  type ObservationalSpec,
  type RecallScope,
  type SemanticRecallSpec,
  type WorkingScope,
  type WorkingSpec,
} from "../knowledgeMemory";
import { ModelPicker } from "@/components/shared/ModelPicker";
import { fieldClass, labelClass } from "./fieldStyles";
import {
  AllowlistSelect,
  FeatureToggle,
  FieldProblem,
  groupClass,
  hintClass,
  JsonTextarea,
  legendClass,
  NumberField,
  RadioGroup,
  useShownOnce,
  type AllowlistOption,
} from "./knowledgeMemoryFields";
import type { EditorStepDefinition, MissingField, StepProps } from "./types";

/** Element id of a memory field (`memory-<field>`), matching `memoryProblems` fields. */
const fieldId = (field: string) => `memory-${field}`;

/** A stored model the loaded list no longer offers (and no wildcard entry of its provider covers). */
function outsideAllowlist(models: ModelOption[] | null, model: { provider: string; name: string } | null | undefined): boolean {
  if (!models || !model || !model.provider || !model.name) return false;
  return !models.some((m) => m.provider === model.provider && (m.wildcard || m.name === model.name));
}

function embeddingOptions(models: EmbeddingModel[]): AllowlistOption[] {
  return models.map((m) => ({
    id: m.id,
    provider: m.provider,
    name: m.name,
    wildcard: m.wildcard,
    label: m.wildcard ? `${m.provider}: any model (type a name, ${m.dimensions} dims)` : `${m.id} (${m.dimensions} dims)`,
  }));
}

function splitModelId(id: string): { provider: string; name: string } {
  const sep = id.indexOf(":");
  return sep < 0 ? { provider: id, name: "" } : { provider: id.slice(0, sep), name: id.slice(sep + 1) };
}

/**
 * Step 7: the agent's memory (`spec.memory`): history limits, working memory, observational memory,
 * semantic recall and redaction. Off leaves `memory` out of the spec entirely.
 */
export function MemoryStep({ spec, draft, onSpecChange }: StepProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const memory = memoryOf(spec);
  const shown = useShownOnce(rootRef, Boolean(memory?.observational || memory?.semantic_recall));
  const [models, setModels] = useState<ModelOption[] | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [embeddings, setEmbeddings] = useState<EmbeddingModel[] | null>(null);
  const [embeddingsError, setEmbeddingsError] = useState<string | null>(null);
  const [catalogKinds, setCatalogKinds] = useState<string[]>([...PII_KINDS]);

  useEffect(() => {
    if (!shown) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchModels())
      .then((items) => {
        if (!cancelled) setModels(items ?? []);
      })
      .catch((error: unknown) => {
        if (!cancelled) setModelsError(error instanceof Error ? error.message : "Failed to load models");
      });
    // The PII kinds the redaction checklist offers (the constant until, or unless, the catalog loads).
    Promise.resolve()
      .then(() => fetchProcessorCatalog())
      .then((catalog) => {
        if (!cancelled) setCatalogKinds(piiKindsFromCatalog(catalog));
      })
      .catch(() => undefined);
    Promise.resolve()
      .then(() => listEmbeddingModels())
      .then((items) => {
        if (!cancelled) setEmbeddings(items ?? []);
      })
      .catch((error: unknown) => {
        if (!cancelled) setEmbeddingsError(error instanceof Error ? error.message : "Failed to load embedding models");
      });
    return () => {
      cancelled = true;
    };
  }, [shown]);

  const problems = memoryProblems(spec);
  const problemFor = (field: string) => problems.find((p) => p.field === field)?.message ?? null;
  const write = (next: MemorySpec | null) => onSpecChange({ memory: memoryValue(next) });
  const patch = (part: Partial<MemorySpec>) => write({ ...(memory ?? {}), ...part });

  const working = memory?.working;
  const observational = memory?.observational;
  const recall = memory?.semantic_recall;
  const setWorking = (p: Partial<WorkingSpec>) => patch({ working: { ...working!, ...p } });
  const setObservational = (p: Partial<ObservationalSpec>) => patch({ observational: { ...observational!, ...p } });
  const setRecall = (p: Partial<SemanticRecallSpec>) => patch({ semantic_recall: { ...recall!, ...p } });
  const firstModel = models?.find((m) => !m.wildcard);
  const firstEmbedding = embeddings?.find((m) => !m.wildcard);
  const inherited = inheritedRedactKinds(draft.inputProcessors);
  const schemaMode = working?.json_schema != null;
  const redactKinds: readonly string[] = Array.isArray(memory?.redact?.kinds) ? memory!.redact!.kinds! : catalogKinds;
  // The catalog's kinds, then any stored kind it does not list (shown, kept, marked unknown).
  const redactOptions = [...catalogKinds, ...redactKinds.filter((kind) => !catalogKinds.includes(kind))];

  return (
    <div ref={rootRef} className="min-w-0 space-y-6">
      <FeatureToggle
        id={fieldId("enabled")}
        label="Use memory"
        description={
          memory
            ? "History is limited to the turns below, and the features you turn on are added."
            : "Off: a conversation replays all its earlier turns, and nothing is remembered across conversations."
        }
        checked={memory !== null}
        onChange={(on) => write(on ? {} : null)}
      />

      {memory ? (
        <>
          <fieldset className={groupClass}>
            <legend className={legendClass}>History</legend>
            <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
              <NumberField
                id={fieldId("history-last_turns")}
                label={`Last turns (1–${HISTORY_MAX_LAST_TURNS})`}
                value={memory.history?.last_turns}
                min={1}
                max={HISTORY_MAX_LAST_TURNS}
                placeholder={`${HISTORY_DEFAULT_LAST_TURNS} (default)`}
                hint="Completed turns kept in context; the oldest are dropped first."
                defaultValue={HISTORY_DEFAULT_LAST_TURNS}
                onChange={(value) => patch({ history: { ...memory.history, last_turns: value } })}
                problem={problemFor("history-last_turns")}
              />
              <NumberField
                id={fieldId("history-max_tokens")}
                label="Max tokens (optional)"
                value={memory.history?.max_tokens}
                min={1}
                max={HISTORY_MAX_TOKENS}
                placeholder="No token budget"
                hint="Whole turns are dropped until the history fits (about 4 characters per token)."
                onChange={(value) => patch({ history: { ...memory.history, max_tokens: value } })}
                problem={problemFor("history-max_tokens")}
              />
            </div>
          </fieldset>

          <fieldset className={groupClass}>
            <legend className={legendClass}>Working memory</legend>
            <FeatureToggle
              id={fieldId("working-enabled")}
              label="Keep a working memory note"
              description="A structured note the agent reads every turn and updates with a tool."
              checked={Boolean(working)}
              onChange={(on) => patch({ working: on ? newWorking() : undefined })}
            />
            {working ? (
              <>
                <RadioGroup<WorkingScope>
                  name="memory-working-scope"
                  firstId={fieldId("working-scope")}
                  legend="Scope"
                  value={working.scope ?? "resource"}
                  options={[
                    { value: "resource", label: "Per user", description: "Shared by all of a user's conversations" },
                    { value: "conversation", label: "Per conversation" },
                  ]}
                  onChange={(scope) => setWorking({ scope })}
                />
                <RadioGroup<"markdown" | "json">
                  name="memory-working-format"
                  firstId={fieldId("working-format")}
                  legend="Shape"
                  value={schemaMode ? "json" : "markdown"}
                  options={[
                    { value: "markdown", label: "Markdown template", description: "The agent rewrites the whole note" },
                    { value: "json", label: "JSON schema", description: "The agent sends changes that are merged in" },
                  ]}
                  onChange={(format) =>
                    setWorking(format === "json" ? { template: undefined, json_schema: STARTER_WORKING_MEMORY_SCHEMA } : { json_schema: undefined, template: undefined })
                  }
                />
                <FieldProblem id={`${fieldId("working-format")}-problem`} message={problemFor("working-format")} />
                {schemaMode ? (
                  <JsonTextarea
                    id={fieldId("working-schema")}
                    label="JSON schema"
                    rows={10}
                    value={working.json_schema}
                    hint={`Supported: object, string, number, integer, boolean, enum, arrays of those; objects nested at most 3 deep, 50 properties, ${MAX_SCHEMA_BYTES / 1024} KiB.`}
                    onChange={(value) => setWorking({ json_schema: value === undefined ? "" : value })}
                    problem={problemFor("working-schema")}
                  />
                ) : (
                  <div className="min-w-0">
                    <label htmlFor={fieldId("working-template")} className={labelClass}>
                      Template (optional)
                    </label>
                    <textarea
                      id={fieldId("working-template")}
                      rows={6}
                      value={working.template ?? ""}
                      placeholder={DEFAULT_WORKING_MEMORY_TEMPLATE}
                      onChange={(e) => setWorking({ template: e.target.value === "" ? undefined : e.target.value })}
                      aria-invalid={problemFor("working-template") ? true : undefined}
                      aria-describedby={`${fieldId("working-template")}-hint ${fieldId("working-template")}-problem`}
                      className={`${fieldClass} font-mono text-caption`}
                    />
                    <p id={`${fieldId("working-template")}-hint`} className={hintClass}>
                      Leave empty to use the default template shown. At most {MAX_TEMPLATE_BYTES / 1024} KiB.
                    </p>
                    <FieldProblem id={`${fieldId("working-template")}-problem`} message={problemFor("working-template")} />
                  </div>
                )}
                <FeatureToggle
                  id={fieldId("working-read_only")}
                  label="Read-only"
                  description="The agent reads the note but cannot change it (edit it in the memory inspector)."
                  checked={Boolean(working.read_only)}
                  onChange={(read_only) => setWorking({ read_only })}
                />
              </>
            ) : null}
          </fieldset>

          <fieldset className={groupClass}>
            <legend className={legendClass}>Observational memory</legend>
            <FeatureToggle
              id={fieldId("observational-enabled")}
              label="Compress older turns into observations"
              description="After each turn a cheap model condenses older turns, so long conversations stay within budget. Per conversation."
              checked={Boolean(observational)}
              onChange={(on) => patch({ observational: on ? newObservational(firstModel ? { provider: firstModel.provider, name: firstModel.name } : null) : undefined })}
            />
            {observational ? (
              <>
                <ModelPicker
                  providerId={fieldId("observational-provider")}
                  modelId={fieldId("observational-model")}
                  providerLabel="Observer provider"
                  modelLabel="Observer model"
                  provider={observational.model?.provider ?? ""}
                  name={observational.model?.name ?? ""}
                  models={models}
                  loadError={modelsError}
                  problem={problemFor("observational-model") ?? (outsideAllowlist(models, observational.model) ? "Not in the platform allowlist: publishing will fail" : null)}
                  hint="From the platform's model allowlist."
                  onChange={({ provider, name }) => setObservational({ model: { provider, name } })}
                />
                <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-3">
                  <NumberField
                    id={fieldId("observational-observe_after_tokens")}
                    label="Observe after (tokens)"
                    value={observational.observe_after_tokens}
                    min={OBSERVE_TOKENS_RANGE[0]}
                    max={OBSERVE_TOKENS_RANGE[1]}
                    step={1000}
                    placeholder={String(OBSERVATIONAL_DEFAULTS.observe_after_tokens)}
                    defaultValue={OBSERVATIONAL_DEFAULTS.observe_after_tokens}
                    onChange={(value) => setObservational({ observe_after_tokens: value })}
                    problem={problemFor("observational-observe_after_tokens")}
                  />
                  <NumberField
                    id={fieldId("observational-keep_recent_turns")}
                    label="Recent turns kept"
                    value={observational.keep_recent_turns}
                    min={KEEP_RECENT_TURNS_RANGE[0]}
                    max={KEEP_RECENT_TURNS_RANGE[1]}
                    placeholder={String(OBSERVATIONAL_DEFAULTS.keep_recent_turns)}
                    defaultValue={OBSERVATIONAL_DEFAULTS.keep_recent_turns}
                    onChange={(value) => setObservational({ keep_recent_turns: value })}
                    problem={problemFor("observational-keep_recent_turns")}
                  />
                  <NumberField
                    id={fieldId("observational-reflect_after_tokens")}
                    label="Reflect after (tokens)"
                    value={observational.reflect_after_tokens}
                    min={OBSERVE_TOKENS_RANGE[0]}
                    max={OBSERVE_TOKENS_RANGE[1]}
                    step={1000}
                    placeholder={String(OBSERVATIONAL_DEFAULTS.reflect_after_tokens)}
                    defaultValue={OBSERVATIONAL_DEFAULTS.reflect_after_tokens}
                    onChange={(value) => setObservational({ reflect_after_tokens: value })}
                    problem={problemFor("observational-reflect_after_tokens")}
                  />
                </div>
              </>
            ) : null}
          </fieldset>

          <fieldset className={groupClass}>
            <legend className={legendClass}>Semantic recall</legend>
            <FeatureToggle
              id={fieldId("recall-enabled")}
              label="Recall related earlier turns"
              description="Finds similar turns from the user's other conversations (or this one) and adds them to the context."
              checked={Boolean(recall)}
              onChange={(on) => patch({ semantic_recall: on ? newSemanticRecall(firstEmbedding?.id ?? null) : undefined })}
            />
            {recall ? (
              <>
                <AllowlistSelect
                  id={fieldId("recall-embedding_model")}
                  label="Embedding model"
                  options={embeddings ? embeddingOptions(embeddings) : null}
                  loadError={embeddingsError}
                  {...splitModelId(recall.embedding_model ?? "")}
                  onChange={(provider, name) => setRecall({ embedding_model: `${provider}:${name}` })}
                  problem={problemFor("recall-embedding_model")}
                  hint="From the platform's embedding model allowlist."
                />
                <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-3">
                  <NumberField
                    id={fieldId("recall-top_k")}
                    label={`Top K (${RECALL_TOP_K_RANGE[0]}–${RECALL_TOP_K_RANGE[1]})`}
                    value={recall.top_k}
                    min={RECALL_TOP_K_RANGE[0]}
                    max={RECALL_TOP_K_RANGE[1]}
                    placeholder={String(RECALL_DEFAULTS.top_k)}
                    defaultValue={RECALL_DEFAULTS.top_k}
                    onChange={(value) => setRecall({ top_k: value })}
                    problem={problemFor("recall-top_k")}
                  />
                  <NumberField
                    id={fieldId("recall-message_range")}
                    label={`Neighbouring turns (${RECALL_MESSAGE_RANGE[0]}–${RECALL_MESSAGE_RANGE[1]})`}
                    value={recall.message_range}
                    min={RECALL_MESSAGE_RANGE[0]}
                    max={RECALL_MESSAGE_RANGE[1]}
                    placeholder={String(RECALL_DEFAULTS.message_range)}
                    defaultValue={RECALL_DEFAULTS.message_range}
                    onChange={(value) => setRecall({ message_range: value })}
                    problem={problemFor("recall-message_range")}
                  />
                  <NumberField
                    id={fieldId("recall-min_score")}
                    label="Minimum score (optional)"
                    value={recall.min_score}
                    min={0}
                    max={1}
                    step={0.05}
                    placeholder="No minimum"
                    onChange={(value) => setRecall({ min_score: value })}
                    problem={problemFor("recall-min_score")}
                  />
                </div>
                <RadioGroup<RecallScope>
                  name="memory-recall-scope"
                  firstId={fieldId("recall-scope")}
                  legend="Search"
                  value={recall.scope ?? "resource"}
                  options={[
                    { value: "resource", label: "The user's conversations" },
                    { value: "conversation", label: "This conversation only" },
                  ]}
                  onChange={(scope) => setRecall({ scope })}
                />
              </>
            ) : null}
          </fieldset>

          <fieldset className={groupClass}>
            <legend className={legendClass}>Redaction</legend>
            <RadioGroup<"inherit" | "custom">
              name="memory-redact-mode"
              legend="Personal data removed before memory is stored"
              value={memory.redact ? "custom" : "inherit"}
              options={[
                {
                  value: "inherit",
                  label: "Same as input redaction",
                  description: inherited.length > 0 ? `Redacts ${inherited.join(", ")} (the input PII processor)` : "No input PII processor: nothing is redacted",
                },
                { value: "custom", label: "Choose kinds" },
              ]}
              onChange={(mode) => patch({ redact: mode === "custom" ? { kinds: [...catalogKinds], action: "redact" } : undefined })}
            />
            {memory.redact ? (
              <fieldset className="min-w-0" aria-describedby={`${fieldId("redact-kinds")}-problem`}>
                <legend className={labelClass}>Kinds</legend>
                <div className="flex min-w-0 flex-wrap gap-2">
                  {redactOptions.map((kind, index) => {
                    const checked = redactKinds.includes(kind);
                    const unknown = !catalogKinds.includes(kind);
                    return (
                      <label key={kind} className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-caption text-foreground">
                        <input
                          type="checkbox"
                          id={index === 0 ? fieldId("redact-kinds") : `${fieldId("redact-kinds")}-${kind}`}
                          className="size-4 accent-primary"
                          checked={checked}
                          onChange={() =>
                            // Every option is kept in place, a stored kind this Studio does not know included.
                            patch({ redact: { ...memory.redact!, kinds: redactOptions.filter((k) => (k === kind ? !checked : redactKinds.includes(k))) } })
                          }
                        />
                        {kind}
                        {unknown ? <span className="text-warning-text"> (unknown)</span> : null}
                      </label>
                    );
                  })}
                </div>
                <FieldProblem id={`${fieldId("redact-kinds")}-problem`} message={problemFor("redact-kinds")} />
              </fieldset>
            ) : null}
          </fieldset>
        </>
      ) : null}
    </div>
  );
}

export const memoryStep: EditorStepDefinition = {
  id: "memory",
  label: "Memory",
  Component: MemoryStep,
  status: memoryStatus,
  ownsError: isMemoryError,
  specKeys: MEMORY_SPEC_KEYS,
  // A warning (`soft`, such as a stored PII kind this Studio does not know) blocks saving only once
  // the user has edited memory; an agent whose memory was never touched still saves.
  missing: (draft, spec): MissingField[] =>
    memoryProblems(spec)
      .filter((problem) => !problem.soft || "memory" in draft.extra)
      .map((problem) => ({ step: "memory", fieldId: fieldId(problem.field), message: problem.message })),
};
