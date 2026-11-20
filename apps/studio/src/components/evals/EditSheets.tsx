"use client";

import React, { useId, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Sheet, SheetBody, SheetContent, SheetFooter, SheetHeader } from "@/components/ui/Sheet";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { useUnsavedChangesGuard, type UnsavedChangesCopy } from "@/components/agents/stepper/useUnsavedChangesGuard";
import {
  MAX_CASE_EVALUATORS,
  MAX_CASE_INPUT_BYTES,
  MAX_DESCRIPTION_CHARS,
  MAX_NAME_CHARS,
  MAX_SUITE_EVALUATORS,
  addCase,
  deleteCase,
  draftsToEvaluators,
  evaluatorApiErrors,
  evaluatorToDraft,
  expectedModeOf,
  expectedText,
  parseExpected,
  parseMetadata,
  updateCase,
  updateSuite,
  type EvalCase,
  type EvalSuite,
  type EvaluatorDraft,
  type ExpectedMode,
} from "@/lib/evals";
import { EvaluatorsEditor } from "./EvaluatorsEditor";
import { ConflictNotice } from "@/components/shared/ConflictNotice";
import { useConflict } from "@/lib/useConflict";
import { alertClass, fieldClass, hintClass, labelClass, monoFieldClass, sectionTitleClass } from "./evalsUi";

export const EVAL_EDIT_GUARD_COPY: UnsavedChangesCopy = {
  title: "Leave without saving?",
  description: "This test has changes that are not saved. Leaving discards them.",
  confirmLabel: "Leave without saving",
};

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

/**
 * Shows a failed save: a 422 item located at `evaluators.N` goes on evaluator row N (the first
 * such row, as the editor shows one evaluator error at a time); everything else goes in the banner.
 */
function showSaveError(
  reason: unknown,
  fallback: string,
  setError: (error: string | null) => void,
  setEvaluatorError: (error: { index: number; error: string } | null) => void,
): void {
  const split = evaluatorApiErrors(reason);
  if (!split || split.rows.size === 0) {
    setError(errorText(reason, fallback));
    return;
  }
  const [index, message] = [...split.rows.entries()].sort((a, b) => a[0] - b[0])[0];
  setEvaluatorError({ index, error: message });
  setError(split.other.length > 0 ? split.other.join(", ") : null);
}

/**
 * Closing a Sheet with unsaved edits asks first; leaving the page asks through the editor's
 * navigation guard. Returns the request-close function and the dialogs to render.
 */
function useDirtyClose(dirty: boolean, onClose: () => void): { requestClose: () => void; dialogs: React.ReactNode } {
  const [asking, setAsking] = useState(false);
  const guard = useUnsavedChangesGuard(dirty, EVAL_EDIT_GUARD_COPY);
  const requestClose = () => (dirty ? setAsking(true) : onClose());
  const dialogs = (
    <>
      {guard}
      {asking ? (
        <ConfirmDialog
          title="Discard changes?"
          description="The changes in this Sheet are not saved."
          confirmLabel="Discard changes"
          destructive
          onConfirm={() => {
            setAsking(false);
            onClose();
          }}
          onCancel={() => setAsking(false)}
        />
      ) : null}
    </>
  );
  return { requestClose, dialogs };
}

// ---------------------------------------------------------------------------- case

interface CaseSheetProps {
  suite: EvalSuite;
  /** The case edited, or null for a new case. */
  record: EvalCase | null;
  onSaved: (record: EvalCase) => void;
  onDeleted: (caseId: string) => void;
  onClose: () => void;
}

interface CaseForm {
  name: string;
  input: string;
  expectedMode: ExpectedMode;
  expected: string;
  metadata: string;
  evaluators: EvaluatorDraft[];
}

function caseForm(record: EvalCase | null): CaseForm {
  const mode = expectedModeOf(record?.expected_output);
  return {
    name: record?.name ?? "",
    input: record?.input ?? "",
    expectedMode: mode,
    expected: expectedText(record?.expected_output, mode),
    metadata: record && Object.keys(record.metadata ?? {}).length > 0 ? JSON.stringify(record.metadata, null, 2) : "",
    evaluators: (record?.evaluators ?? []).map(evaluatorToDraft),
  };
}

function formKey(form: CaseForm): string {
  return JSON.stringify({ ...form, evaluators: form.evaluators.map(({ key: _key, ...rest }) => rest) });
}

/** The case editor (`?edit=<case id>` or `?edit=new`): name, input, expected output as text or JSON,
 * metadata and the case's own evaluators. */
export function CaseSheet({ suite, record, onSaved, onDeleted, onClose }: CaseSheetProps) {
  const id = useId();
  // `base` is what "unsaved" is measured against; 重新載入 after a 412 moves it to the current version.
  const [base, setBase] = useState(record);
  const initial = useMemo(() => caseForm(base), [base]);
  const [form, setForm] = useState<CaseForm>(initial);
  const conflict = useConflict<EvalCase>({ onSaved: (saved) => onSaved(saved as EvalCase) });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evaluatorError, setEvaluatorError] = useState<{ index: number; error: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const dirty = formKey(form) !== formKey(initial);
  const { requestClose, dialogs } = useDirtyClose(dirty && !saving, onClose);
  const set = (patch: Partial<CaseForm>) => setForm((f) => ({ ...f, ...patch }));
  const readOnly = suite.archived;

  const setMode = (mode: ExpectedMode) => {
    // Carry the text across: Text → JSON quotes it, JSON → Text unquotes a JSON string.
    let text = form.expected;
    if (form.expectedMode === "text" && mode === "json") text = form.expected ? JSON.stringify(form.expected) : "";
    else if (form.expectedMode === "json" && mode === "text") {
      try {
        const parsed = JSON.parse(form.expected);
        text = typeof parsed === "string" ? parsed : form.expected;
      } catch {
        // Keep the text as it is.
      }
    }
    set({ expectedMode: mode, expected: mode === "none" ? "" : text });
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || readOnly) return;
    setError(null);
    setEvaluatorError(null);
    const name = form.name.trim();
    if (!name) return setError("Enter a case name");
    if (!form.input.trim()) return setError("Enter the input the agent receives");
    if (new TextEncoder().encode(form.input).length > MAX_CASE_INPUT_BYTES) return setError("The input must be at most 32 KiB");
    const expected = parseExpected(form.expectedMode, form.expected);
    if ("error" in expected) return setError(expected.error);
    const metadata = parseMetadata(form.metadata);
    if ("error" in metadata) return setError(metadata.error);
    const evaluators = draftsToEvaluators(form.evaluators);
    if ("error" in evaluators) return setEvaluatorError({ index: evaluators.index, error: evaluators.error });
    const body = { name, input: form.input, expected_output: expected.value, metadata: metadata.value, evaluators: evaluators.evaluators };
    setSaving(true);
    try {
      const saved = record ? await conflict.guard(() => updateCase(suite.id, record.id, body)) : await addCase(suite.id, body);
      if (saved === undefined) return setSaving(false); // held as a conflict notice
      onSaved(saved);
    } catch (reason: unknown) {
      showSaveError(reason, "The case could not be saved", setError, setEvaluatorError);
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!record) return;
    setDeleteError(null);
    try {
      await deleteCase(suite.id, record.id);
      setConfirmDelete(false);
      onDeleted(record.id);
    } catch (reason: unknown) {
      setDeleteError(errorText(reason, "The case could not be deleted"));
    }
  };

  return (
    <Sheet open onOpenChange={(open) => { if (!open) requestClose(); }}>
      <SheetContent size="md" data-testid="eval-case-sheet" aria-describedby={undefined}>
        <SheetHeader title={record ? `Case ${record.name}` : "New case"} />
        <form onSubmit={(e) => void save(e)} className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label={record ? "Edit case" : "New case"}>
          <SheetBody className="space-y-4">
            {readOnly ? <p className={hintClass}>The suite is archived: restore it to edit its cases.</p> : null}
            <ConflictNotice
              state={conflict}
              onReload={(current) => {
                if (!current) return;
                setBase(current);
                setForm(caseForm(current));
                setError(null);
              }}
            />
            {error ? <p role="alert" className={alertClass} data-testid="eval-case-error">{error}</p> : null}
            <fieldset disabled={readOnly || saving} className="min-w-0 space-y-4">
              <div className="space-y-1">
                <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
                <input id={`${id}-name`} className={fieldClass} value={form.name} maxLength={MAX_NAME_CHARS} onChange={(e) => set({ name: e.target.value })} data-testid="eval-case-name" />
                <p className={hintClass}>Unique in the suite; an import updates the case with the same name.</p>
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-input`} className={labelClass}>Input</label>
                <textarea id={`${id}-input`} rows={5} className={fieldClass} value={form.input} onChange={(e) => set({ input: e.target.value })} data-testid="eval-case-input" />
                <p className={hintClass}>The user message the agent answers, as one turn.</p>
              </div>
              <div className="space-y-2">
                <span className={labelClass} id={`${id}-expected-label`}>Expected output</span>
                <SegmentedControl<ExpectedMode>
                  label="Expected output format"
                  size="sm"
                  value={form.expectedMode}
                  onValueChange={setMode}
                  options={[
                    { value: "none", label: "None" },
                    { value: "text", label: "Text" },
                    { value: "json", label: "JSON" },
                  ]}
                />
                {form.expectedMode === "none" ? null : (
                  <textarea
                    aria-labelledby={`${id}-expected-label`}
                    rows={form.expectedMode === "json" ? 6 : 4}
                    className={form.expectedMode === "json" ? monoFieldClass : fieldClass}
                    value={form.expected}
                    onChange={(e) => set({ expected: e.target.value })}
                    data-testid="eval-case-expected"
                    data-mode={form.expectedMode}
                  />
                )}
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-metadata`} className={labelClass}>Metadata <span className="font-normal text-muted-foreground">(optional JSON object)</span></label>
                <textarea id={`${id}-metadata`} rows={3} className={monoFieldClass} value={form.metadata} onChange={(e) => set({ metadata: e.target.value })} placeholder='{"topic": "billing"}' data-testid="eval-case-metadata" />
              </div>
              <div className="space-y-2">
                <h3 className={sectionTitleClass}>Case evaluators</h3>
                <p className={hintClass}>Run in addition to the suite&apos;s evaluators, for this case only.</p>
                <EvaluatorsEditor
                  drafts={form.evaluators}
                  onChange={(evaluators) => set({ evaluators })}
                  max={MAX_CASE_EVALUATORS}
                  errorIndex={evaluatorError?.index ?? null}
                  error={evaluatorError?.error ?? null}
                  disabled={readOnly || saving}
                  testId="case-evaluators"
                />
              </div>
            </fieldset>
          </SheetBody>
          <SheetFooter>
            {record && !readOnly ? (
              <Button type="button" variant="ghost" className="mr-auto text-destructive-text" onClick={() => setConfirmDelete(true)} data-testid="eval-case-delete">
                Delete case
              </Button>
            ) : null}
            <Button type="button" variant="ghost" onClick={requestClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={saving || readOnly} data-testid="eval-case-save">
              {saving ? "Saving…" : record ? "Save case" : "Add case"}
            </Button>
          </SheetFooter>
        </form>
        {dialogs}
        {confirmDelete && record ? (
          <ConfirmDialog
            title={`Delete case ${record.name}?`}
            description="Past eval runs keep their results for it."
            confirmLabel="Delete case"
            destructive
            error={deleteError}
            onConfirm={() => void remove()}
            onCancel={() => setConfirmDelete(false)}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

// ---------------------------------------------------------------------------- suite

interface SuiteSheetProps {
  suite: EvalSuite;
  onSaved: (suite: EvalSuite) => void;
  onClose: () => void;
}

interface SuiteForm {
  name: string;
  description: string;
  evaluators: EvaluatorDraft[];
}

function suiteForm(suite: EvalSuite): SuiteForm {
  return { name: suite.name, description: suite.description ?? "", evaluators: suite.evaluators.map(evaluatorToDraft) };
}

function suiteKey(form: SuiteForm): string {
  return JSON.stringify({ ...form, evaluators: form.evaluators.map(({ key: _key, ...rest }) => rest) });
}

/** The suite editor (`?edit=suite`): name, description and the evaluators every case runs. */
export function SuiteSheet({ suite, onSaved, onClose }: SuiteSheetProps) {
  const id = useId();
  // `base` is what "unsaved" is measured against; 重新載入 after a 412 moves it to the current version.
  const [base, setBase] = useState(suite);
  const initial = useMemo(() => suiteForm(base), [base]);
  const [form, setForm] = useState<SuiteForm>(initial);
  const conflict = useConflict<EvalSuite>({ onSaved: (saved) => onSaved(saved as EvalSuite) });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evaluatorError, setEvaluatorError] = useState<{ index: number; error: string } | null>(null);
  const dirty = suiteKey(form) !== suiteKey(initial);
  const { requestClose, dialogs } = useDirtyClose(dirty && !saving, onClose);
  const set = (patch: Partial<SuiteForm>) => setForm((f) => ({ ...f, ...patch }));
  const readOnly = suite.archived;

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || readOnly) return;
    setError(null);
    setEvaluatorError(null);
    const name = form.name.trim();
    if (!name) return setError("Enter a suite name");
    const evaluators = draftsToEvaluators(form.evaluators);
    if ("error" in evaluators) return setEvaluatorError({ index: evaluators.index, error: evaluators.error });
    setSaving(true);
    try {
      const saved = await conflict.guard(() => updateSuite(suite.id, { name, description: form.description.trim() || null, evaluators: evaluators.evaluators }));
      if (saved === undefined) return setSaving(false); // held as a conflict notice
      onSaved(saved);
    } catch (reason: unknown) {
      showSaveError(reason, "The suite could not be saved", setError, setEvaluatorError);
      setSaving(false);
    }
  };

  return (
    <Sheet open onOpenChange={(open) => { if (!open) requestClose(); }}>
      <SheetContent size="md" data-testid="eval-suite-sheet" aria-describedby={undefined}>
        <SheetHeader title="Edit suite" />
        <form onSubmit={(e) => void save(e)} className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="Edit suite">
          <SheetBody className="space-y-4">
            {readOnly ? <p className={hintClass}>The suite is archived: restore it to edit it.</p> : null}
            <ConflictNotice
              state={conflict}
              onReload={(current) => {
                if (!current) return;
                setBase(current);
                setForm(suiteForm(current));
                setError(null);
              }}
            />
            {error ? <p role="alert" className={alertClass} data-testid="eval-suite-error">{error}</p> : null}
            <fieldset disabled={readOnly || saving} className="min-w-0 space-y-4">
              <div className="space-y-1">
                <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
                <input id={`${id}-name`} className={fieldClass} value={form.name} maxLength={MAX_NAME_CHARS} onChange={(e) => set({ name: e.target.value })} data-testid="eval-suite-name" />
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-description`} className={labelClass}>Description <span className="font-normal text-muted-foreground">(optional)</span></label>
                <textarea id={`${id}-description`} rows={3} className={fieldClass} value={form.description} maxLength={MAX_DESCRIPTION_CHARS} onChange={(e) => set({ description: e.target.value })} data-testid="eval-suite-description" />
              </div>
              <div className="space-y-2">
                <h3 className={sectionTitleClass}>Evaluators</h3>
                <p className={hintClass}>Every case runs these. A suite edit never changes a running or past eval run.</p>
                <EvaluatorsEditor
                  drafts={form.evaluators}
                  onChange={(evaluators) => set({ evaluators })}
                  max={MAX_SUITE_EVALUATORS}
                  errorIndex={evaluatorError?.index ?? null}
                  error={evaluatorError?.error ?? null}
                  disabled={readOnly || saving}
                  testId="suite-evaluators"
                />
              </div>
            </fieldset>
          </SheetBody>
          <SheetFooter>
            <Button type="button" variant="ghost" onClick={requestClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={saving || readOnly} data-testid="eval-suite-save">
              {saving ? "Saving…" : "Save suite"}
            </Button>
          </SheetFooter>
        </form>
        {dialogs}
      </SheetContent>
    </Sheet>
  );
}
