"use client";

import React, { useEffect, useId, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { ModelPicker } from "@/components/shared/ModelPicker";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { fetchModels, type ModelOption } from "@/components/agents/specSections";
import {
  EVALUATOR_KINDS,
  EVALUATOR_LABELS,
  blankEvaluatorDraft,
  judgeModelIds,
  type EvaluatorDraft,
  type EvaluatorKind,
} from "@/lib/evals";
import { alertClass, cardClass, fieldClass, hintClass, labelClass, monoFieldClass } from "./evalsUi";
import { Select } from "@/components/ui/Select";

/** The judge models offered (allowlisted concrete models from `GET /models`), loaded once per editor. */
export function useJudgeModels(): { models: string[]; options: ModelOption[] | null; loaded: boolean; error: string | null } {
  const [models, setModels] = useState<string[]>([]);
  const [options, setOptions] = useState<ModelOption[] | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchModels())
      .then((items: ModelOption[]) => {
        if (cancelled) return;
        const list = Array.isArray(items) ? items : [];
        setModels(judgeModelIds(list));
        setOptions(list);
        setLoaded(true);
      })
      .catch(() => {
        if (cancelled) return;
        setError("the request failed");
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return { models, options, loaded, error };
}

interface EvaluatorsEditorProps {
  drafts: EvaluatorDraft[];
  onChange: (drafts: EvaluatorDraft[]) => void;
  max: number;
  /** The draft with a problem (from `draftsToEvaluators`), and the problem. */
  errorIndex?: number | null;
  error?: string | null;
  disabled?: boolean;
  /** The test id prefix (`suite-evaluators`, `case-evaluators`). */
  testId: string;
}

/** A list of evaluators: kind plus its settings, add and remove. The judge has a model picker. */
export function EvaluatorsEditor({ drafts, onChange, max, errorIndex = null, error = null, disabled = false, testId }: EvaluatorsEditorProps) {
  const judge = useJudgeModels();
  const update = (index: number, patch: Partial<EvaluatorDraft>) => onChange(drafts.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  const remove = (index: number) => onChange(drafts.filter((_, i) => i !== index));
  const add = () => onChange([...drafts, blankEvaluatorDraft("equals_expected", judge.models[0] ?? "")]);
  return (
    <div className="min-w-0 space-y-3" data-testid={testId}>
      {drafts.length === 0 ? <p className={hintClass}>No evaluators.</p> : null}
      {drafts.map((draft, index) => (
        <EvaluatorRow
          key={draft.key}
          draft={draft}
          index={index}
          judgeModels={judge.models}
          judgeOptions={judge.options}
          judgeModelsLoaded={judge.loaded}
          judgeModelsError={judge.error}
          onChange={(patch) => update(index, patch)}
          onRemove={() => remove(index)}
          error={errorIndex === index ? error : null}
          disabled={disabled}
        />
      ))}
      <Button type="button" size="sm" onClick={add} disabled={disabled || drafts.length >= max} data-testid="evaluator-add">
        <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        Add evaluator
      </Button>
      {drafts.length >= max ? <p className={hintClass}>At most {max} evaluators.</p> : null}
    </div>
  );
}

interface EvaluatorRowProps {
  draft: EvaluatorDraft;
  index: number;
  judgeModels: string[];
  /** The `GET /models` items (null until loaded or when they could not be). */
  judgeOptions: ModelOption[] | null;
  /** The list has loaded (or failed): only then does a stored model's absence from it mean anything. */
  judgeModelsLoaded: boolean;
  judgeModelsError: string | null;
  onChange: (patch: Partial<EvaluatorDraft>) => void;
  onRemove: () => void;
  error: string | null;
  disabled: boolean;
}

function EvaluatorRow({ draft, index, judgeModels, judgeOptions, judgeModelsError, onChange, onRemove, error, disabled }: EvaluatorRowProps) {
  const id = useId();
  const field = (suffix: string) => `${id}-${suffix}`;
  const setKind = (kind: EvaluatorKind) => onChange({ kind, model: kind === "llm_judge" && !draft.model ? judgeModels[0] ?? "" : draft.model });
  const sep = draft.model.indexOf(":");
  const judgeProvider = sep < 0 ? draft.model : draft.model.slice(0, sep);
  const judgeName = sep < 0 ? "" : draft.model.slice(sep + 1);
  return (
    <fieldset className={`${cardClass} space-y-3 p-3`} data-testid="evaluator-row" data-index={index} data-kind={draft.kind} disabled={disabled}>
      <legend className="sr-only">Evaluator {index + 1}</legend>
      <div className="flex min-w-0 flex-wrap items-end gap-2">
        <div className="min-w-0 flex-1 space-y-1">
          <label htmlFor={field("kind")} className={labelClass}>Evaluator {index + 1}</label>
          <Select id={field("kind")} className={fieldClass} value={draft.kind} onValueChange={(v) => setKind(v as EvaluatorKind)} data-testid="evaluator-kind">
            {EVALUATOR_KINDS.map((kind) => (
              <option key={kind} value={kind}>{EVALUATOR_LABELS[kind]}</option>
            ))}
          </Select>
        </div>
        <div className="min-w-0 flex-1 space-y-1">
          <label htmlFor={field("name")} className={labelClass}>Name <span className="font-normal text-muted-foreground">(optional)</span></label>
          <input id={field("name")} className={fieldClass} value={draft.name} maxLength={100} onChange={(e) => onChange({ name: e.target.value })} placeholder={draft.kind} data-testid="evaluator-name" />
        </div>
        <IconButton label={`Remove evaluator ${index + 1}`} size="sm" onClick={onRemove} icon={<Trash2 strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} data-testid="evaluator-remove" />
      </div>

      {draft.kind === "contains" ? (
        <>
          <div className="space-y-1">
            <label htmlFor={field("value")} className={labelClass}>Text</label>
            <input id={field("value")} value={draft.value} onChange={(e) => onChange({ value: e.target.value })} placeholder={draft.valueIsJson ? "42, true, [1, 2] or {\"a\": 1}" : "Empty: the expected output"} className={draft.valueIsJson ? monoFieldClass : fieldClass} data-testid="evaluator-value" />
          </div>
          <label className="flex items-center gap-2 text-label text-foreground">
            <input type="checkbox" checked={draft.caseSensitive} onChange={(e) => onChange({ caseSensitive: e.target.checked })} data-testid="evaluator-case-sensitive" />
            Case sensitive
          </label>
          <label className="flex items-center gap-2 text-label text-foreground">
            <input type="checkbox" checked={draft.valueIsJson} onChange={(e) => onChange({ valueIsJson: e.target.checked })} data-testid="evaluator-value-json" />
            Value is JSON (a number, true or false, a list or an object)
          </label>
        </>
      ) : null}

      {draft.kind === "regex" ? (
        <>
          <div className="space-y-1">
            <label htmlFor={field("pattern")} className={labelClass}>Pattern (RE2)</label>
            <input id={field("pattern")} className={monoFieldClass} value={draft.pattern} maxLength={200} onChange={(e) => onChange({ pattern: e.target.value })} data-testid="evaluator-pattern" />
          </div>
          <label className="flex items-center gap-2 text-label text-foreground">
            <input type="checkbox" checked={draft.negate} onChange={(e) => onChange({ negate: e.target.checked })} data-testid="evaluator-negate" />
            Must not match (passes when the pattern is absent)
          </label>
          <div className="space-y-1">
            <label htmlFor={field("regex-field")} className={labelClass}>Field path <span className="font-normal text-muted-foreground">(optional)</span></label>
            <input id={field("regex-field")} className={monoFieldClass} value={draft.regexField} onChange={(e) => onChange({ regexField: e.target.value })} placeholder="Empty: the whole output. Example: post" data-testid="evaluator-regex-field" />
          </div>
        </>
      ) : null}

      {draft.kind === "schema_valid" ? (
        <div className="space-y-1">
          <label htmlFor={field("schema")} className={labelClass}>JSON Schema <span className="font-normal text-muted-foreground">(optional)</span></label>
          <textarea id={field("schema")} rows={4} className={monoFieldClass} value={draft.jsonSchema} onChange={(e) => onChange({ jsonSchema: e.target.value })} placeholder="Empty: the agent's output schema" data-testid="evaluator-schema" />
        </div>
      ) : null}

      {draft.kind === "field_equals" ? (
        <div className="flex min-w-0 flex-wrap gap-2">
          <div className="min-w-0 flex-1 space-y-1">
            <label htmlFor={field("path")} className={labelClass}>Field path</label>
            <input id={field("path")} className={monoFieldClass} value={draft.path} onChange={(e) => onChange({ path: e.target.value })} placeholder="answer.priority" data-testid="evaluator-path" />
          </div>
          <div className="min-w-0 flex-1 space-y-1">
            <label htmlFor={field("fvalue")} className={labelClass}>Value (JSON)</label>
            <input id={field("fvalue")} className={monoFieldClass} value={draft.value} onChange={(e) => onChange({ value: e.target.value })} placeholder='"high"' data-testid="evaluator-field-value" />
          </div>
        </div>
      ) : null}

      {draft.kind === "max_duration" ? (
        <div className="space-y-1">
          <label htmlFor={field("seconds")} className={labelClass}>Seconds</label>
          <input id={field("seconds")} inputMode="decimal" className={fieldClass} value={draft.seconds} onChange={(e) => onChange({ seconds: e.target.value })} data-testid="evaluator-seconds" />
        </div>
      ) : null}

      {draft.kind === "max_cost" ? (
        <div className="space-y-1">
          <label htmlFor={field("usd")} className={labelClass}>USD</label>
          <input id={field("usd")} inputMode="decimal" className={fieldClass} value={draft.usd} onChange={(e) => onChange({ usd: e.target.value })} placeholder="0.01" data-testid="evaluator-usd" />
        </div>
      ) : null}

      {draft.kind === "llm_judge" ? (
        <>
          <div className="space-y-1">
            <label htmlFor={field("rubric")} className={labelClass}>Rubric</label>
            <textarea id={field("rubric")} rows={4} className={fieldClass} value={draft.rubric} onChange={(e) => onChange({ rubric: e.target.value })} placeholder="The answer is polite and names the order number." data-testid="evaluator-rubric" />
          </div>
          <div className="space-y-1">
            <ModelPicker
              providerId={field("provider")}
              modelId={field("model")}
              providerLabel="Judge provider"
              modelLabel="Judge model"
              provider={judgeProvider}
              name={judgeName}
              models={judgeOptions}
              loadError={judgeModelsError}
              onChange={({ provider, name }) => onChange({ model: provider || name ? `${provider}:${name}` : "" })}
            />
            <p className={hintClass}>Only allowed models can judge; the judge has no fallback models.</p>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            <label className="flex items-center gap-2 text-label text-foreground">
              <input type="checkbox" checked={draft.includeInput} onChange={(e) => onChange({ includeInput: e.target.checked })} data-testid="evaluator-include-input" />
              Show the judge the input
            </label>
            <label className="flex items-center gap-2 text-label text-foreground">
              <input type="checkbox" checked={draft.includeExpected} onChange={(e) => onChange({ includeExpected: e.target.checked })} data-testid="evaluator-include-expected" />
              Show the judge the expected output
            </label>
          </div>
          <div className="space-y-1">
            <label htmlFor={field("threshold")} className={labelClass}>Pass threshold <span className="font-normal text-muted-foreground">(optional, 0 to 1)</span></label>
            <input id={field("threshold")} inputMode="decimal" className={fieldClass} value={draft.passThreshold} onChange={(e) => onChange({ passThreshold: e.target.value })} data-testid="evaluator-threshold" />
          </div>
        </>
      ) : null}

      {error ? <p role="alert" className={alertClass} data-testid="evaluator-error">{error}</p> : null}
    </fieldset>
  );
}
