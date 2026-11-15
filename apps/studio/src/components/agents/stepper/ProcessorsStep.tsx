"use client";

import React, { useEffect, useState } from "react";
import { ProcessorsEditor } from "../ProcessorsEditor";
import {
  errorTarget,
  fetchModels,
  fetchProcessorCatalog,
  stepForErrorMessage,
  stepStatus,
  type ModelOption,
  type ProcessorCatalog,
} from "../specSections";
import type { EditorStepDefinition, StepProps } from "./types";
import { readableError } from "@/components/agents/readableError";

/** Step 5: input and output processors (guardrails). */
export function ProcessorsStep({ draft, onChange, errors }: StepProps) {
  const [catalog, setCatalog] = useState<ProcessorCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelOption[] | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);
  // A server error naming `<phase>_processors[i]` is pinned to that processor's clientId when it
  // arrives, so it follows the processor through reordering and disappears when it is removed.
  const [pinned, setPinned] = useState<{ phase: "input" | "output"; clientId: string; message: string } | null>(null);
  const { serverError } = errors;

  useEffect(() => {
    let cancelled = false;
    fetchProcessorCatalog()
      .then((value) => {
        if (!cancelled) setCatalog(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) setCatalogError(error instanceof Error ? error.message : "Failed to load processors");
      });
    fetchModels()
      .then((value) => {
        if (!cancelled) setModels(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) setModelsError(error instanceof Error ? error.message : "Failed to load models");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Only a new serverError re-pins; edits to the lists afterwards must not move the message.
  useEffect(() => {
    const target = serverError ? errorTarget(serverError) : null;
    if (!serverError || target?.kind !== "processor") {
      setPinned(null);
      return;
    }
    const list = target.phase === "input" ? draft.inputProcessors : draft.outputProcessors;
    const processor = list[target.index];
    setPinned(processor ? { phase: target.phase, clientId: processor.clientId, message: serverError } : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverError]);

  const pinnedErrors = (phase: "input" | "output"): Record<string, string> =>
    pinned && pinned.phase === phase ? { [pinned.clientId]: readableError(pinned.message) } : {};

  return (
    <ProcessorsEditor
      inputProcessors={draft.inputProcessors}
      outputProcessors={draft.outputProcessors}
      onInputChange={(inputProcessors) => onChange({ inputProcessors })}
      onOutputChange={(outputProcessors) => onChange({ outputProcessors })}
      catalog={catalog}
      catalogError={catalogError}
      models={models}
      modelsError={modelsError}
      sectionError={errors.target?.kind === "processors" && serverError ? readableError(serverError) : null}
      inputErrors={pinnedErrors("input")}
      outputErrors={pinnedErrors("output")}
    />
  );
}

export const processorsStep: EditorStepDefinition = {
  id: "processors",
  label: "Processors",
  Component: ProcessorsStep,
  status: (spec) => stepStatus("processors", spec)!,
  ownsError: (message) => stepForErrorMessage(message) === "processors",
};
