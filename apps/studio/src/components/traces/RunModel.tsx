"use client";

import React from "react";
import { runModels } from "@/lib/traces";

/**
 * A run's model cell (the Traces lists): the model that answered, with the requested one after it
 * where a fallback model answered. `-` without any model.
 */
export function RunModel({ run, testId }: { run: { model_id?: unknown; answering_model_id?: unknown }; testId?: string }) {
  const { model, requested } = runModels(run);
  if (!model) return <span className="text-muted-foreground" data-testid={testId}>-</span>;
  return (
    <span data-testid={testId}>
      {model}
      {requested ? (
        <span className="ml-1 text-muted-foreground" title={`Requested ${requested}; a fallback model answered`} data-testid={testId ? `${testId}-requested` : undefined}>
          (requested {requested})
        </span>
      ) : null}
    </span>
  );
}
