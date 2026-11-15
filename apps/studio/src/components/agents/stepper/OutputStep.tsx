"use client";

/**
 * Step 8 (AC.13): structured output (`spec.output`). Off leaves `output` out of the spec entirely, so
 * the agent answers in text exactly as before. On, every answer is a JSON object that matches the
 * schema, edited in the field builder or as raw JSON Schema (both check the subset inline; the API
 * re-checks on save and publish). Tokens only.
 */
import React, { useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import {
  CITATIONS_DESCRIPTION,
  DEFAULT_OUTPUT_MAX_RETRIES,
  effectiveOutputMode,
  hasCitationsField,
  isOutputError,
  MAX_OUTPUT_DESCRIPTION_CHARS,
  MAX_OUTPUT_RETRIES,
  OUTPUT_SPEC_KEYS,
  outputOf,
  outputProblems,
  outputStatus,
  outputValue,
  STARTER_OUTPUT_SCHEMA,
  withCitationsField,
  type OutputMode,
  type OutputSpecValue,
} from "../agentCapabilities";
import { fieldClass, labelClass } from "./fieldStyles";
import { FeatureToggle, FieldProblem, groupClass, hintClass, JsonTextarea, legendClass, NumberField, RadioGroup } from "./knowledgeMemoryFields";
import { OutputSchemaBuilder } from "./OutputSchemaBuilder";
import type { EditorStepDefinition, MissingField, StepProps } from "./types";

/** Element id of an output field (`output-<field>`); `output-schema-problems` is the schema's summary. */
const fieldId = (field: string) => (field === "schema" ? "output-schema-problems" : `output-${field}`);

const MODE_OPTIONS: { value: OutputMode; label: string; description: string }[] = [
  { value: "tool", label: "Tool (default)", description: "The model answers by calling an output tool. Works with every model." },
  { value: "native", label: "Native (advanced)", description: "The provider's JSON Schema output. Runs fail if the model does not support it." },
  { value: "prompted", label: "Prompted (advanced)", description: "The schema is put in the instructions and the answer is parsed as JSON." },
];

export function OutputStep({ spec, onSpecChange }: StepProps) {
  const output = outputOf(spec);
  const [tab, setTab] = useState("fields");
  const problems = outputProblems(spec);
  const problemFor = (field: string) => problems.find((p) => p.field === field)?.message ?? null;
  const write = (next: OutputSpecValue | null) => onSpecChange({ output: outputValue(next) });
  const patch = (part: Partial<OutputSpecValue>) => write({ ...(output ?? { json_schema: STARTER_OUTPUT_SCHEMA }), ...part });
  const schemaProblem = problemFor("schema");
  const citations = output ? hasCitationsField(output.json_schema) : false;

  return (
    <div className="min-w-0 space-y-6">
      <FeatureToggle
        id="output-enabled"
        label="Structured output"
        description={
          output
            ? "Every answer is a JSON object that matches the schema below. A wrong answer is sent back to the model with the errors."
            : "Off: the agent answers in text."
        }
        checked={output !== null}
        onChange={(on) => write(on ? { json_schema: STARTER_OUTPUT_SCHEMA } : null)}
      />

      {output ? (
        <>
          <fieldset className={groupClass}>
            <legend className={legendClass}>Fields</legend>
            <div
              id={fieldId("schema")}
              tabIndex={-1}
              role="status"
              data-testid="output-schema-problems"
              className={schemaProblem ? "rounded-md border border-destructive/40 bg-destructive/10 p-2 text-caption text-destructive-text" : "sr-only"}
            >
              {schemaProblem ?? ""}
            </div>
            <Tabs value={tab} onValueChange={setTab}>
              <TabsList aria-label="Output schema editor">
                <TabsTrigger value="fields">Fields</TabsTrigger>
                <TabsTrigger value="raw">JSON Schema</TabsTrigger>
              </TabsList>
              <TabsContent value="fields" className="pt-3">
                <OutputSchemaBuilder schema={output.json_schema} onChange={(json_schema) => patch({ json_schema })} />
              </TabsContent>
              <TabsContent value="raw" className="pt-3">
                <JsonTextarea
                  id="output-json_schema"
                  label="JSON Schema"
                  rows={14}
                  value={output.json_schema}
                  onChange={(json_schema) => patch({ json_schema })}
                  hint='The root is "type": "object". Not supported: $ref, $defs, oneOf, anyOf, allOf, not, pattern.'
                  problem={schemaProblem}
                />
              </TabsContent>
            </Tabs>
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <Button size="sm" disabled={citations} onClick={() => patch({ json_schema: withCitationsField(output.json_schema) })}>
                <Plus aria-hidden="true" />
                Add citations field
              </Button>
              <span className={hintClass} id="output-citations-hint">
                {citations ? "The schema has a citations field." : `Adds citations: list of integer, "${CITATIONS_DESCRIPTION}" (for agents with knowledge).`}
              </span>
            </div>
          </fieldset>

          <fieldset className={groupClass}>
            <legend className={legendClass}>How the model answers</legend>
            <RadioGroup
              name="output-mode"
              firstId="output-mode"
              legend="Mode"
              value={effectiveOutputMode(output)}
              options={MODE_OPTIONS}
              // Tool is the default: it is saved as no `mode` at all.
              onChange={(mode) => patch({ mode: mode === "tool" ? undefined : mode })}
            />
            <NumberField
              id="output-max_retries"
              label={`Retries (0–${MAX_OUTPUT_RETRIES})`}
              value={output.max_retries}
              min={0}
              max={MAX_OUTPUT_RETRIES}
              placeholder={`${DEFAULT_OUTPUT_MAX_RETRIES} (default)`}
              hint="How many times a wrong answer is sent back before the run fails output_invalid."
              onChange={(max_retries) => patch({ max_retries })}
              problem={problemFor("max_retries")}
            />
            <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="min-w-0">
                <label htmlFor="output-name" className={labelClass}>
                  Output name (optional)
                </label>
                <input
                  id="output-name"
                  type="text"
                  placeholder="output"
                  value={output.name ?? ""}
                  onChange={(e) => patch({ name: e.target.value })}
                  aria-invalid={problemFor("name") ? true : undefined}
                  aria-describedby="output-name-problem"
                  className={`${fieldClass} font-mono`}
                />
                <FieldProblem id="output-name-problem" message={problemFor("name")} />
              </div>
              <div className="min-w-0">
                <label htmlFor="output-description" className={labelClass}>
                  Output description (optional)
                </label>
                <input
                  id="output-description"
                  type="text"
                  maxLength={MAX_OUTPUT_DESCRIPTION_CHARS + 1}
                  value={output.description ?? ""}
                  onChange={(e) => patch({ description: e.target.value })}
                  aria-invalid={problemFor("description") ? true : undefined}
                  aria-describedby="output-description-problem"
                  className={fieldClass}
                />
                <FieldProblem id="output-description-problem" message={problemFor("description")} />
              </div>
            </div>
          </fieldset>
        </>
      ) : null}
    </div>
  );
}

export const outputStep: EditorStepDefinition = {
  id: "output",
  label: "Output",
  Component: OutputStep,
  status: outputStatus,
  ownsError: isOutputError,
  specKeys: OUTPUT_SPEC_KEYS,
  missing: (_draft, spec): MissingField[] => outputProblems(spec).map((problem) => ({ step: "output", fieldId: fieldId(problem.field), message: problem.message })),
};
