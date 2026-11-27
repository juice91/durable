"use client";

/**
 * The Architect page's workflow form: describe a workflow and let the drafter fill it in, or build it as
 * rows (agent, parallel and tool steps, run in order), see the graph it compiles to, save a draft or save
 * and go live, and once it is live put it on a schedule. Everything is the restricted format of
 * `lib/architect-workflow.ts`; the API validates and compiles the real workflow. Tokens only.
 */
import { PermissionNotice } from "@/components/access/PermissionNotice";
import { useCan } from "@/lib/permissions";
import React, { useCallback, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { PublishWarnings } from "@/components/agents/PublishWarnings";
import { WorkflowCanvas } from "@/components/workflows/WorkflowGraph";
import { cronPreview } from "@/components/workflows/cronPreview";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { DrafterFailure } from "./ArchitectModel";
import { createSchedule, type WorkflowSchedule } from "@/lib/schedules";
import {
  ArchitectApiError,
  MAX_BRANCHES,
  MAX_WORKFLOW_STEPS,
  MIN_BRANCHES,
  compilePreview,
  draftWorkflow,
  emptySpec,
  newStep,
  nextStepId,
  placeholdersFor,
  problemsFor,
  rowsFromSteps,
  saveWorkflow,
  stepsFromRows,
  type StepProblems,
  type WfAgentListItem,
  type WfOptions,
  type WfRow,
  type WfSaved,
  type WfSpec,
} from "@/lib/architect-workflow";
import { Select } from "@/components/ui/Select";

export interface WorkflowFormProps {
  /** GET /architect/options: the tools and the Architect-made agents a step may use. */
  options: WfOptions;
  /** GET /architect `agents`: the Architect-made agents, to name the ones a step still points at. */
  agents: readonly WfAgentListItem[];
  /** An existing workflow: its restricted spec and its latest version (null: never published). */
  initial?: { spec: WfSpec; version: number | null };
  /** After every successful save. */
  onSaved?: (saved: WfSaved, spec: WfSpec) => void;
}

const field = "w-full rounded-md border border-input bg-background px-3 py-2 text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const labelClass = "text-label font-medium text-foreground";
const alertClass = "rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text";
const NO_PROBLEMS: StepProblems = { byStep: {}, general: [] };

const KIND_LABELS = { agent: "Agent", parallel: "Parallel", tool: "Tool" } as const;

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** An agent picker; an agent the step points at that is not offered stays selectable and says so. */
function AgentSelect({ id, value, onChange, options, agents, label = "Agent" }: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  options: WfOptions["agents"];
  agents: readonly WfAgentListItem[];
  label?: string;
}) {
  const offered = options.some((a) => a.id === value);
  const known = agents.find((a) => a.id === value);
  return (
    <div className="space-y-1">
      <label htmlFor={id} className={labelClass}>{label}</label>
      <Select id={id} value={value} onValueChange={(v) => onChange(v)} className={field}>
        <option value="">Choose an agent</option>
        {options.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.id})</option>)}
        {value && !offered ? <option value={value}>{known?.name ?? value} ({value}, not available)</option> : null}
      </Select>
    </div>
  );
}

/** A prompt box with the placeholders that are valid at this step. */
function PromptField({ id, value, onChange, placeholders }: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  placeholders: readonly string[];
}) {
  const ref = React.useRef<HTMLTextAreaElement>(null);
  const insert = (token: string) => {
    if (!token) return;
    const box = ref.current;
    const at = box?.selectionStart ?? value.length;
    const end = box?.selectionEnd ?? at;
    onChange(`${value.slice(0, at)}${token}${value.slice(end)}`);
  };
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className={labelClass}>Prompt</label>
        <Select aria-label="Insert placeholder" value="" onValueChange={(v) => insert(v)} className="rounded-md border border-input bg-background px-2 py-1 text-caption text-foreground">
          <option value="">Insert placeholder</option>
          {placeholders.map((p) => <option key={p} value={p}>{p}</option>)}
        </Select>
      </div>
      <textarea ref={ref} id={id} value={value} onChange={(e) => onChange(e.target.value)} rows={3} maxLength={4000} className={`${field} font-mono`} />
    </div>
  );
}

function Problems({ messages }: { messages: readonly string[] | undefined }) {
  if (!messages || messages.length === 0) return null;
  return (
    <div role="alert" className={`${alertClass} space-y-1`}>
      {messages.map((m) => <p key={m}>{m}</p>)}
    </div>
  );
}

const CREATE_ACTION = ["create"] as const;

export function WorkflowForm({ options, agents, initial, onSaved }: WorkflowFormProps) {
  const [name, setName] = useState(initial?.spec.name ?? "");
  const [slug, setSlug] = useState(initial?.spec.id ?? "");
  const [description, setDescription] = useState(initial?.spec.description ?? "");
  const [inputDescription, setInputDescription] = useState(initial?.spec.input_description ?? emptySpec().input_description);
  const [rows, setRows] = useState<WfRow[]>(() => rowsFromSteps(initial?.spec.steps ?? []));
  const [version, setVersion] = useState<number | null>(initial?.version ?? null);

  const [describe, setDescribe] = useState("");
  const [filling, setFilling] = useState(false);
  const [assumptions, setAssumptions] = useState<string[]>([]);
  const [dropped, setDropped] = useState<{ kind: string; id: string }[]>([]);
  const [fillError, setFillError] = useState<string[] | null>(null);
  const [unusable, setUnusable] = useState<{ answer: string | null; problems: string[] } | null>(null);

  const [saving, setSaving] = useState<"draft" | "publish" | null>(null);
  const [saved, setSaved] = useState<WfSaved | null>(null);
  const [problems, setProblems] = useState<StepProblems>(NO_PROBLEMS);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [paramErrors, setParamErrors] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState<{ latest: number | null; message: string } | null>(null);

  const { steps } = useMemo(() => stepsFromRows(rows), [rows]);
  const current: WfSpec = useMemo(
    () => ({ name, id: slug.trim() || null, description, input_description: inputDescription, steps }),
    [name, slug, description, inputDescription, steps],
  );
  const preview = useMemo(() => compilePreview(current), [current]);
  const defaultAgent = options.agents[0]?.id ?? "";

  const patchRow = useCallback((key: string, patch: Partial<WfRow>) => {
    setRows((all) => all.map((row) => (row.key === key ? ({ ...row, ...patch } as WfRow) : row)));
  }, []);
  const patchBranch = (key: string, branchKey: string, patch: Partial<{ id: string; agent: string; prompt: string }>) => {
    setRows((all) => all.map((row) => (row.key === key && row.kind === "parallel" ? { ...row, branches: row.branches.map((b) => (b.key === branchKey ? { ...b, ...patch } : b)) } : row)));
  };
  const move = (index: number, by: -1 | 1) => {
    setRows((all) => {
      const to = index + by;
      if (to < 0 || to >= all.length) return all;
      const next = [...all];
      [next[index], next[to]] = [next[to], next[index]];
      return next;
    });
  };
  const addRow = (kind: "agent" | "parallel" | "tool") => {
    setRows((all) => {
      const [row] = rowsFromSteps([newStep(kind, stepsFromRows(all).steps, defaultAgent)]);
      return [...all, row];
    });
  };

  const fillIn = async () => {
    if (!describe.trim() || filling) return;
    setFilling(true);
    setFillError(null);
    setUnusable(null);
    try {
      const result = await draftWorkflow(describe.trim(), current.steps.length > 0 ? current : null);
      setName(result.spec.name ?? "");
      setSlug(result.spec.id ?? "");
      setDescription(result.spec.description ?? "");
      setInputDescription(result.spec.input_description ?? emptySpec().input_description);
      setRows(rowsFromSteps(result.spec.steps ?? []));
      setAssumptions(result.assumptions ?? []);
      setDropped(result.dropped ?? []);
      setProblems(NO_PROBLEMS);
      setFormErrors([]);
      setSaved(null);
    } catch (reason: unknown) {
      if (reason instanceof ArchitectApiError && (reason.code === "draft_invalid" || reason.code === "draft_failed")) {
        const answer = reason.raw ?? (typeof reason.body.error === "string" ? reason.body.error : null);
        setUnusable({ answer, problems: reason.problems });
      } else {
        setFillError([reason instanceof Error ? reason.message : "The drafter could not answer."]);
      }
    } finally {
      setFilling(false);
    }
  };

  // The API creates a new workflow or edits and publishes the one named; `create` covers drafting and a new one.
  const mayCreate = useCan("workflow", null, "create");
  const save = async (publish: boolean, expected: number | null = version) => {
    if (saving) return;
    const built = stepsFromRows(rows);
    const own: string[] = [];
    if (!name.trim()) own.push("Give the workflow a name.");
    if (built.steps.length === 0) own.push("Add at least one step.");
    for (const { problems: list } of [preview]) for (const p of list) own.push(p.message);
    setParamErrors(built.errors);
    if (own.length > 0 || Object.keys(built.errors).length > 0) {
      setFormErrors(own);
      return;
    }
    setSaving(publish ? "publish" : "draft");
    setFormErrors([]);
    setProblems(NO_PROBLEMS);
    setConflict(null);
    try {
      const result = await saveWorkflow({ ...current, steps: built.steps }, { publish, expectedVersion: expected });
      setSaved(result);
      setVersion(result.version);
      setSlug(result.id);
      onSaved?.(result, { ...current, id: result.id, steps: built.steps });
    } catch (reason: unknown) {
      if (reason instanceof ArchitectApiError && reason.code === "version_mismatch") {
        setConflict({ latest: reason.latestVersion, message: reason.message });
      } else if (reason instanceof ArchitectApiError && reason.status === 422) {
        setProblems(problemsFor(reason, { ...current, steps: built.steps }));
      } else {
        setFormErrors([reason instanceof Error ? reason.message : "Could not save the workflow."]);
      }
    } finally {
      setSaving(null);
    }
  };

  const stepNumber = (index: number) => index + 1;

  return (
    <div className="flex flex-col gap-6" data-testid="workflow-form">
      <section aria-labelledby="wf-describe-title" className="space-y-2">
        <h2 id="wf-describe-title" className="text-title font-medium text-foreground">Describe</h2>
        <label htmlFor="wf-describe" className={labelClass}>Describe the workflow</label>
        <textarea id="wf-describe" value={describe} onChange={(e) => setDescribe(e.target.value)} rows={3} maxLength={8000} placeholder="What should happen, in what order, and which agents do it" className={field} />
        <div className="flex items-center gap-2">
          {mayCreate ? (
            <Button variant="secondary" onClick={fillIn} disabled={filling || !describe.trim()}>{filling ? "Drafting…" : "Fill in the form"}</Button>
          ) : null}
          <p className="text-caption text-muted-foreground">The drafter fills the form below; nothing is saved until you save.</p>
        </div>
        {unusable ? <DrafterFailure answer={unusable.answer} problems={unusable.problems} /> : null}
        {fillError ? (
          <div role="alert" className={`${alertClass} space-y-1`}>
            {fillError.map((m) => <p key={m}>{m}</p>)}
          </div>
        ) : null}
        {assumptions.length > 0 ? (
          <div className="rounded-md border border-border bg-surface p-3 text-label text-foreground" data-testid="workflow-assumptions">
            <p className="font-medium">Assumptions</p>
            <ul className="mt-1 list-inside list-disc space-y-0.5">{assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
          </div>
        ) : null}
        {dropped.length > 0 ? (
          <p className="rounded-md border border-warning/40 bg-warning/10 p-2 text-label text-warning-text">
            Left out because they are not available here: {dropped.map((d) => `${d.kind} ${d.id}`).join(", ")}.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="wf-details-title" className="grid gap-3 sm:grid-cols-2">
        <h2 id="wf-details-title" className="text-title font-medium text-foreground sm:col-span-2">Workflow</h2>
        <div className="space-y-1">
          <label htmlFor="wf-name" className={labelClass}>Name</label>
          <input id="wf-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} className={field} />
        </div>
        <div className="space-y-1">
          <label htmlFor="wf-id" className={labelClass}>Id</label>
          <input id="wf-id" value={slug} onChange={(e) => setSlug(e.target.value)} readOnly={saved !== null || version !== null} placeholder="Made from the name" spellCheck={false} className={`${field} font-mono`} />
        </div>
        <div className="space-y-1">
          <label htmlFor="wf-description" className={labelClass}>Description</label>
          <input id="wf-description" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={300} className={field} />
        </div>
        <div className="space-y-1">
          <label htmlFor="wf-input-description" className={labelClass}>What it is given</label>
          <input id="wf-input-description" value={inputDescription} onChange={(e) => setInputDescription(e.target.value)} maxLength={300} className={field} />
        </div>
      </section>

      <section aria-labelledby="wf-steps-title" className="space-y-3">
        <h2 id="wf-steps-title" className="text-title font-medium text-foreground">Steps</h2>
        <p className="text-caption text-muted-foreground">Steps run in order. A parallel step runs its agents together and the next step joins them.</p>
        {rows.length === 0 ? <p className="rounded-md border border-dashed border-border p-3 text-label text-muted-foreground">No steps yet. Add one below, or describe the workflow above.</p> : null}
        {rows.map((row, index) => {
          const own = problems.byStep[row.id];
          const rowSteps = steps;
          return (
            <fieldset key={row.key} aria-invalid={own ? true : undefined} className={`space-y-3 rounded-md border bg-card p-3 ${own ? "border-destructive/60" : "border-border"}`}>
              <legend className="px-1 text-label font-medium text-foreground">{`Step ${stepNumber(index)} · ${KIND_LABELS[row.kind]}`}</legend>
              <div className="flex items-end gap-2">
                <div className="min-w-0 flex-1 space-y-1">
                  <label htmlFor={`${row.key}-id`} className={labelClass}>Step id</label>
                  <input id={`${row.key}-id`} value={row.id} onChange={(e) => patchRow(row.key, { id: e.target.value })} maxLength={32} spellCheck={false} className={`${field} font-mono`} />
                </div>
                <IconButton label={`Move step ${stepNumber(index)} up`} onClick={() => move(index, -1)} disabled={index === 0} icon={<ArrowUp strokeWidth={ICON_STROKE_WIDTH} />} />
                <IconButton label={`Move step ${stepNumber(index)} down`} onClick={() => move(index, 1)} disabled={index === rows.length - 1} icon={<ArrowDown strokeWidth={ICON_STROKE_WIDTH} />} />
                <IconButton label={`Remove step ${stepNumber(index)}`} onClick={() => setRows((all) => all.filter((r) => r.key !== row.key))} icon={<Trash2 strokeWidth={ICON_STROKE_WIDTH} />} />
              </div>

              {row.kind === "agent" ? (
                <>
                  <AgentSelect id={`${row.key}-agent`} value={row.agent} onChange={(agent) => patchRow(row.key, { agent })} options={options.agents} agents={agents} />
                  <PromptField id={`${row.key}-prompt`} value={row.prompt} onChange={(prompt) => patchRow(row.key, { prompt })} placeholders={placeholdersFor(rowSteps, index)} />
                </>
              ) : null}

              {row.kind === "tool" ? (
                <>
                  <div className="space-y-1">
                    <label htmlFor={`${row.key}-tool`} className={labelClass}>Tool</label>
                    <Select id={`${row.key}-tool`} value={row.tool} onValueChange={(v) => patchRow(row.key, { tool: v })} className={field}>
                      <option value="">Choose a tool</option>
                      {options.tools.map((t) => <option key={t.id} value={t.id}>{t.title} ({t.id}){t.requires_approval ? " · needs approval" : ""}</option>)}
                      {row.tool && !options.tools.some((t) => t.id === row.tool) ? <option value={row.tool}>{row.tool} (not available)</option> : null}
                    </Select>
                  </div>
                  <div className="space-y-1">
                    <label htmlFor={`${row.key}-params`} className={labelClass}>Parameters</label>
                    <textarea id={`${row.key}-params`} value={row.paramsText} onChange={(e) => patchRow(row.key, { paramsText: e.target.value })} rows={3} spellCheck={false} placeholder='{"text": "{{previous}}"}' className={`${field} font-mono`} />
                    <p className="text-caption text-muted-foreground">A JSON object. A value that uses a placeholder is exactly that placeholder: {placeholdersFor(rowSteps, index).join(", ")}.</p>
                    <Problems messages={paramErrors[row.key] ? [paramErrors[row.key]] : undefined} />
                  </div>
                </>
              ) : null}

              {row.kind === "parallel" ? (
                <div className="space-y-3">
                  {row.branches.map((branch, b) => (
                    <div key={branch.key} className={`space-y-2 rounded-md border bg-surface p-3 ${problems.byStep[branch.id] ? "border-destructive/60" : "border-border"}`}>
                      <div className="flex items-end gap-2">
                        <div className="min-w-0 flex-1 space-y-1">
                          <label htmlFor={`${branch.key}-id`} className={labelClass}>{`Branch ${b + 1} id`}</label>
                          <input id={`${branch.key}-id`} value={branch.id} onChange={(e) => patchBranch(row.key, branch.key, { id: e.target.value })} maxLength={32} spellCheck={false} className={`${field} font-mono`} />
                        </div>
                        <IconButton label={`Remove branch ${b + 1} of step ${stepNumber(index)}`} disabled={row.branches.length <= MIN_BRANCHES} onClick={() => patchRow(row.key, { branches: row.branches.filter((x) => x.key !== branch.key) } as Partial<WfRow>)} icon={<Trash2 strokeWidth={ICON_STROKE_WIDTH} />} />
                      </div>
                      <AgentSelect id={`${branch.key}-agent`} value={branch.agent} onChange={(agent) => patchBranch(row.key, branch.key, { agent })} options={options.agents} agents={agents} label={`Branch ${b + 1} agent`} />
                      <PromptField id={`${branch.key}-prompt`} value={branch.prompt} onChange={(prompt) => patchBranch(row.key, branch.key, { prompt })} placeholders={placeholdersFor(rowSteps, index)} />
                      <Problems messages={problems.byStep[branch.id]} />
                    </div>
                  ))}
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={row.branches.length >= MAX_BRANCHES}
                    onClick={() => {
                      const id = nextStepId(rowSteps, "branch");
                      patchRow(row.key, { branches: [...row.branches, { key: `${row.key}-b${row.branches.length + 1}-${id}`, id, agent: defaultAgent, prompt: "{{input}}" }] } as Partial<WfRow>);
                    }}
                  >
                    <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />Add branch
                  </Button>
                </div>
              ) : null}
              <Problems messages={own} />
            </fieldset>
          );
        })}
        <div className="flex flex-wrap gap-2">
          {(["agent", "parallel", "tool"] as const).map((kind) => (
            <Button key={kind} variant="secondary" size="sm" disabled={rows.length >= MAX_WORKFLOW_STEPS} onClick={() => addRow(kind)}>
              <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />{`Add ${kind} step`}
            </Button>
          ))}
          {rows.length >= MAX_WORKFLOW_STEPS ? <p className="self-center text-caption text-muted-foreground">A workflow has at most {MAX_WORKFLOW_STEPS} steps.</p> : null}
        </div>
      </section>

      <section aria-labelledby="wf-preview-title" className="space-y-2">
        <h2 id="wf-preview-title" className="text-title font-medium text-foreground">Preview</h2>
        <div data-testid="workflow-preview" className="h-80 overflow-hidden rounded-md border border-border bg-background">
          {steps.length === 0 ? (
            <p className="p-3 text-label text-muted-foreground">Add a step to see the workflow as a graph.</p>
          ) : (
            <WorkflowCanvas spec={preview.spec} />
          )}
        </div>
      </section>

      {problems.general.length > 0 || formErrors.length > 0 ? (
        <div role="alert" className={`${alertClass} space-y-1`}>
          {[...formErrors, ...problems.general].map((m) => <p key={m}>{m}</p>)}
        </div>
      ) : null}

      {conflict ? (
        <div data-testid="workflow-version-conflict" className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-label text-warning-text">
          <p>
            {conflict.latest !== null ? `This workflow is now at version ${conflict.latest}; this form was last saved against version ${version ?? "none"}.` : "This workflow changed since the form was loaded."}{" "}
            Saving again replaces that version&apos;s draft with this form.
          </p>
          {conflict.latest !== null ? (
            <Button variant="secondary" size="sm" onClick={() => { const latest = conflict.latest; setVersion(latest); void save(false, latest); }}>{`Save over version ${conflict.latest}`}</Button>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        {mayCreate ? (
          <Button variant="secondary" onClick={() => void save(false)} disabled={saving !== null}>{saving === "draft" ? "Saving…" : "Save draft"}</Button>
        ) : null}
        {mayCreate ? (
          <Button onClick={() => void save(true)} disabled={saving !== null}>{saving === "publish" ? "Publishing…" : "Save and go live"}</Button>
        ) : null}
      </div>

      {mayCreate ? null : <PermissionNotice kind="workflow" id={null} actions={CREATE_ACTION} />}

      {saved ? (
        <div className="space-y-2" aria-live="polite">
          <p className="text-label text-foreground">
            {saved.published ? `Live as version ${saved.version}.` : "Saved as a draft. Nothing that is running changed."}
          </p>
          <PublishWarnings warnings={saved.warnings} />
        </div>
      ) : null}

      {saved?.published ? <ScheduleBlock workflowId={saved.workflow_id} /> : null}
    </div>
  );
}

/** A schedule for the workflow just put live (the round 2 schedules API): it runs the latest version on a cron. */
function ScheduleBlock({ workflowId }: { workflowId: string }) {
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [timezone, setTimezone] = useState(browserTimezone);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<WorkflowSchedule | null>(null);

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setCreated(await createSchedule(workflowId, { name: name.trim(), cron: cron.trim(), timezone: timezone.trim(), input: { input } }));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Failed to create the schedule");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={create}>
      <fieldset className="space-y-3 rounded-md border border-border bg-card p-3">
        <legend className="px-1 text-label font-medium text-foreground">Schedule</legend>
        <p className="text-caption text-muted-foreground">Run the live version on a cron. You can pause or change it later on the workflow&apos;s Schedules page.</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor="wf-schedule-name" className={labelClass}>Name</label>
            <input id="wf-schedule-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} className={field} />
          </div>
          <div className="space-y-1">
            <label htmlFor="wf-schedule-cron" className={labelClass}>Cron</label>
            <input id="wf-schedule-cron" value={cron} onChange={(e) => setCron(e.target.value)} spellCheck={false} aria-describedby="wf-schedule-cron-preview" className={`${field} font-mono`} />
            <p id="wf-schedule-cron-preview" className="text-caption text-muted-foreground">{cronPreview(cron)}</p>
          </div>
          <div className="space-y-1">
            <label htmlFor="wf-schedule-timezone" className={labelClass}>Timezone</label>
            <input id="wf-schedule-timezone" value={timezone} onChange={(e) => setTimezone(e.target.value)} spellCheck={false} className={field} />
          </div>
          <div className="space-y-1">
            <label htmlFor="wf-schedule-input" className={labelClass}>Input</label>
            <input id="wf-schedule-input" value={input} onChange={(e) => setInput(e.target.value)} className={field} />
          </div>
        </div>
        {error ? <p role="alert" className={alertClass}>{error}</p> : null}
        {created ? <p className="text-label text-success-text">{`Schedule "${created.name}" created.`}{created.next_fire_at ? ` Next run ${created.next_fire_at}.` : ""}</p> : null}
        <Button type="submit" variant="secondary" disabled={busy || !name.trim() || !cron.trim()}>{busy ? "Creating…" : "Create schedule"}</Button>
      </fieldset>
    </form>
  );
}
