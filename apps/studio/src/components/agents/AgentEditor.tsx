"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AgentSpec } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { errorTarget, type StepStatus } from "./specSections";
import { AgentStepper } from "./stepper/AgentStepper";
import { checkExtraKeys, draftDiffers, draftFromSpec, draftToSpec, type EditorDraft } from "./stepper/editorDraft";
import type { EditorLayout } from "./stepper/editorFit";
import { EDITOR_STEPS, missingFields, stepForError } from "./stepper/registry";
import type { EditorStepDefinition, StepProps, StepServices } from "./stepper/types";

/** The stepped layout (SL.11): which step shows, and what goes around it. */
export interface AgentEditorStepper {
  activeStep: string;
  onStepChange: (id: string) => void;
  layout: EditorLayout;
  /** Header actions after the submit button (Test draft, Publish). */
  actions?: React.ReactNode;
  notices?: React.ReactNode;
  /** The versions column (edit mode). */
  versions?: React.ReactNode;
  /** The submit button's look: primary when it is the page's main action (Create agent). */
  submitVariant?: "primary" | "secondary";
}

interface AgentEditorProps {
  initialData?: AgentSpec;
  onSubmit: (spec: AgentSpec) => Promise<void>;
  submitLabel: string;
  onDirtyChange?: (isDirty: boolean) => void;
  /**
   * The last save or publish error from the API, if any. Messages that name a processor
   * (`input_processors[0]...`), the instructions (`instructions: ...`) or a processor model are
   * shown next to that part of the form; in the stepper, the step holding it opens.
   */
  serverError?: string | null;
  /** Show the steps one at a time in the stepper. Without it every step shows, in order, as one form. */
  stepper?: AgentEditorStepper;
  /** The steps (default: the registry's). */
  steps?: readonly EditorStepDefinition[];
}

export const AGENT_EDITOR_FORM_ID = "agent-editor-form";

/**
 * The agent spec editor. It owns the form state (`EditorDraft`) and renders each registered step
 * (`stepper/registry.ts`) with it. Every step stays mounted; under the stepper only the active one
 * is shown, so a step's own state (loaded MCP tools, an open picker) survives switching steps.
 */
export function AgentEditor({ initialData, onSubmit, submitLabel, onDirtyChange, serverError = null, stepper, steps = EDITOR_STEPS }: AgentEditorProps) {
  const [draft, setDraft] = useState<EditorDraft>(() => draftFromSpec(initialData));
  const [submitting, setSubmitting] = useState(false);
  const instructionsRef = useRef<HTMLTextAreaElement>(null);
  const formRef = useRef<HTMLFormElement>(null);

  const spec = useMemo(() => draftToSpec(draft, initialData), [draft, initialData]);
  const dirty = useMemo(() => draftDiffers(draft, initialData), [draft, initialData]);

  // The editor is the one source of the dirty state: reported on mount, whenever it changes, and
  // again for every new stored spec (a save that returns while the user kept typing stays dirty).
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, initialData, onDirtyChange]);

  const onChange = useCallback((patch: Partial<EditorDraft>) => setDraft((current) => ({ ...current, ...patch })), []);
  const onSpecChange = useCallback(
    (patch: Record<string, unknown>) => {
      const allowed = checkExtraKeys(patch);
      setDraft((current) => ({ ...current, extra: { ...current.extra, ...allowed } }));
    },
    [],
  );

  const insertPlaceholder = useCallback((fieldName: string) => {
    const token = `{{context.${fieldName}}}`;
    const element = instructionsRef.current;
    setDraft((current) => {
      const start = element?.selectionStart ?? current.instructions.length;
      const end = element?.selectionEnd ?? current.instructions.length;
      return { ...current, instructions: current.instructions.slice(0, start) + token + current.instructions.slice(end) };
    });
  }, []);
  const services = useMemo<StepServices>(() => ({ instructionsRef, insertPlaceholder }), [insertPlaceholder]);

  const errors = useMemo(() => ({ serverError, target: serverError ? errorTarget(serverError) : null }), [serverError]);
  const errorStep = stepForError(serverError, steps);

  // A new save or publish error opens the step it belongs to.
  const onStepChange = stepper?.onStepChange;
  useEffect(() => {
    if (errorStep && onStepChange) onStepChange(errorStep);
    // Only when the error changes, not when the step does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverError]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    if (stepper) {
      // Hidden steps' inputs cannot show the browser's own message: open the step first.
      const missing = missingFields(draft, spec, steps)[0];
      if (missing) {
        if (missing.step !== stepper.activeStep) stepper.onStepChange(missing.step);
        setTimeout(() => {
          const element = document.getElementById(missing.fieldId);
          if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
            element.setCustomValidity(missing.message ?? "");
            element.focus();
            element.reportValidity();
            element.setCustomValidity("");
          }
        }, 0);
        return;
      }
    }
    setSubmitting(true);
    try {
      await onSubmit(draftToSpec(draft, initialData));
    } finally {
      setSubmitting(false);
    }
  };

  // A step change the user started (nav, select, Previous / Next) moves focus to the new step's
  // heading; one opened for them (an error, a missing field) does not.
  const userStepChange = useRef(false);
  const headingRefs = useRef<Record<string, HTMLHeadingElement | null>>({});
  const activeStep = stepper?.activeStep;
  useEffect(() => {
    if (!userStepChange.current || !activeStep) return;
    userStepChange.current = false;
    const heading = headingRefs.current[activeStep];
    heading?.focus();
    heading?.scrollIntoView?.({ block: "start" });
  }, [activeStep]);
  const onUserStepChange = useCallback(
    (id: string) => {
      userStepChange.current = true;
      onStepChange?.(id);
    },
    [onStepChange],
  );

  // An error a step owns but places nowhere itself (no `errorTarget`: an MCP server, the model, a
  // later step's key) is shown at the top of that step.
  const stepAlert = serverError && errorStep && !errors.target ? serverError : null;

  const stepProps: StepProps = { draft, spec, onChange, onSpecChange, errors, services };
  const bodies = steps.map((step) => {
    const shown = !stepper || step.id === stepper.activeStep;
    const titleId = `editor-step-${step.id}-title`;
    return (
      <section key={step.id} aria-labelledby={titleId} hidden={!shown} data-step={step.id} className="min-w-0 space-y-4">
        <h2
          id={titleId}
          ref={(node) => {
            headingRefs.current[step.id] = node;
          }}
          tabIndex={-1}
          className="rounded-sm text-title font-semibold text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {step.label}
        </h2>
        {stepAlert && errorStep === step.id ? (
          <div role="alert" data-testid="agent-save-error" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-label text-destructive-text">
            {stepAlert}
          </div>
        ) : null}
        <step.Component {...stepProps} />
      </section>
    );
  });

  const submitButton = (
    <Button type="submit" disabled={submitting} variant={stepper ? (stepper.submitVariant ?? "secondary") : "primary"} size={stepper ? "sm" : "lg"} className={stepper ? undefined : "w-full"}>
      {submitting ? "Saving..." : submitLabel}
    </Button>
  );

  if (!stepper) {
    return (
      <form ref={formRef} id={AGENT_EDITOR_FORM_ID} onSubmit={handleSubmit} className="min-w-0 space-y-8">
        {bodies}
        <div className="pt-4">{submitButton}</div>
      </form>
    );
  }

  const statuses: Record<string, StepStatus> = {};
  for (const step of steps) statuses[step.id] = step.status(spec);

  return (
    <form ref={formRef} id={AGENT_EDITOR_FORM_ID} onSubmit={handleSubmit} noValidate className="min-w-0">
      <AgentStepper
        steps={steps}
        statuses={statuses}
        errorSteps={errorStep ? [errorStep] : []}
        activeStep={stepper.activeStep}
        onStepChange={onUserStepChange}
        layout={stepper.layout}
        actions={
          <>
            {submitButton}
            {stepper.actions}
          </>
        }
        notices={stepper.notices}
        versions={stepper.versions}
      >
        {bodies}
      </AgentStepper>
    </form>
  );
}
