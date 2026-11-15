"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Download, MoreHorizontal, Pencil, Play, Plus, Upload } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/DropdownMenu";
import { IconButton } from "@/components/ui/IconButton";
import { toast } from "@/components/ui/Toaster";
import { useUrlSheet } from "@/components/ui/useUrlSheet";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { StatusPill } from "@/components/runs/StatusPill";
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";
import type { Agent, AgentVersion } from "@/lib/api";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { formatCost, formatDuration } from "@/lib/format";
import { routes } from "@/lib/routes";
import type { RunStatus } from "@/lib/runs";
import {
  MAX_IMPORT_BYTES,
  MAX_SUITE_CASES,
  deleteSuite,
  downloadText,
  evaluatorLabel,
  evaluatorSummary,
  exportSuite,
  formatRate,
  getSuite,
  importFormatOf,
  importSuite,
  runCompareParam,
  runTargetLabel,
  updateSuite,
  type EvalCase,
  type EvalRun,
  type EvalSuiteWithCases,
  type ExportFormat,
} from "@/lib/evals";
import { CaseSheet, SuiteSheet } from "./EditSheets";
import { RunDialog } from "./RunDialog";
import { rememberCaseCount } from "./useEvalRun";
import { alertClass, cardClass, fieldClass, hintClass, labelClass, sectionTitleClass } from "./evalsUi";

/** The editor Sheet's query parameter: `suite` (the suite), `new` (a new case) or a case id. */
export const EDIT_PARAM = "edit";

interface SuiteViewProps {
  agent: Agent;
  versions: AgentVersion[];
  suiteId: string;
  hasMemory: boolean;
  runs: EvalRun[] | null;
  runsError: string | null;
  reloadRuns: () => void;
}

function preview(text: string, max = 80): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function expectedPreview(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return preview(typeof value === "string" ? value : JSON.stringify(value), 60);
}

/** One suite: its evaluators, cases (a row opens the case Sheet), run history with compare, and the
 * Run, Import and Export actions. */
export function SuiteView({ agent, versions, suiteId, hasMemory, runs, runsError, reloadRuns }: SuiteViewProps) {
  const router = useRouter();
  const [suite, setSuite] = useState<EvalSuiteWithCases | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [running, setRunning] = useState(false);
  const [confirm, setConfirm] = useState<"delete" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [base, setBase] = useState("");
  const [target, setTarget] = useState("");
  const fileRef = useRef<HTMLInputElement | null>(null);
  const edit = useUrlSheet(EDIT_PARAM, { history: "push" });

  const load = useCallback(() => setReloadKey((k) => k + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    getSuite(suiteId, controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) setSuite(loaded);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error && reason.message ? reason.message : "The suite could not be loaded");
      });
    return () => controller.abort();
  }, [suiteId, reloadKey]);

  const archived = suite?.archived === true;
  const canAddCase = suite !== null && !archived && suite.cases.length < MAX_SUITE_CASES;
  useCreateAction(canAddCase ? () => edit.open("new") : null);

  const suiteRuns = (runs ?? []).filter((r) => r.suite_id === suiteId);

  const onExport = async (format: ExportFormat) => {
    setActionError(null);
    try {
      const file = await exportSuite(suiteId, format);
      downloadText(file.filename, file.text, file.type);
    } catch (reason: unknown) {
      setActionError(reason instanceof Error && reason.message ? reason.message : "The suite could not be exported");
    }
  };

  const onImportFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setImportError(null);
    // Refuse a large file before reading it into memory (the API's cap; importSuite re-checks the bytes).
    if (file.size > MAX_IMPORT_BYTES) {
      setImportError("the suite file must be at most 1 MiB");
      return;
    }
    setImporting(true);
    try {
      const text = await file.text();
      const result = await importSuite(suiteId, text, importFormatOf(file.name));
      toast.success(`Imported: ${result.created} new, ${result.updated} updated`);
      load();
    } catch (reason: unknown) {
      setImportError(reason instanceof Error && reason.message ? reason.message : "The file could not be imported");
    } finally {
      setImporting(false);
    }
  };

  const setArchived = async (value: boolean) => {
    setActionError(null);
    try {
      await updateSuite(suiteId, { archived: value });
      load();
    } catch (reason: unknown) {
      setActionError(reason instanceof Error && reason.message ? reason.message : "The suite could not be changed");
    }
  };

  const onDelete = async () => {
    setActionError(null);
    try {
      await deleteSuite(suiteId);
      setConfirm(null);
      toast.success("Suite deleted");
      router.push(routes.agentTests(agent.id));
    } catch (reason: unknown) {
      setActionError(reason instanceof Error && reason.message ? reason.message : "The suite could not be deleted");
      setConfirm(null);
    }
  };

  const caseColumns: DataListColumn<EvalCase>[] = [
    { id: "name", header: "Case", cell: (c) => c.name, sortable: true, sortValue: (c) => c.name, className: "min-w-0 max-w-[14rem] truncate" },
    { id: "input", header: "Input", cell: (c) => <span className="line-clamp-2 break-words text-muted-foreground">{preview(c.input)}</span>, className: "hidden min-w-0 max-w-[24rem] md:table-cell" },
    { id: "expected", header: "Expected", cell: (c) => <span className="line-clamp-2 break-words font-mono text-muted-foreground">{expectedPreview(c.expected_output)}</span>, className: "hidden min-w-0 max-w-[16rem] lg:table-cell" },
    { id: "evaluators", header: "Own evaluators", cell: (c) => c.evaluators.length || "—", align: "end", className: "hidden sm:table-cell tabular-nums" },
  ];

  const runColumns: DataListColumn<EvalRun>[] = [
    { id: "started", header: "Started", cell: (r) => new Date(r.created_at).toLocaleString(), sortable: true, sortValue: (r) => r.created_at, className: "whitespace-nowrap" },
    { id: "target", header: "Target", cell: (r) => runTargetLabel(r), sortable: true, sortValue: (r) => r.version ?? 0 },
    { id: "status", header: "Status", cell: (r) => <StatusPill status={r.display_status as RunStatus} /> },
    { id: "pass", header: "Pass rate", cell: (r) => formatRate(r.aggregates?.pass_rate), sortable: true, sortValue: (r) => r.aggregates?.pass_rate ?? null, align: "end", className: "tabular-nums" },
    { id: "cases", header: "Cases", cell: (r) => r.aggregates?.cases ?? "—", align: "end", className: "hidden sm:table-cell tabular-nums" },
    { id: "p50", header: "Median", cell: (r) => formatDuration(r.aggregates?.duration_ms_p50), align: "end", className: "hidden md:table-cell tabular-nums" },
    { id: "cost", header: "Cost", cell: (r) => formatCost(r.aggregates?.cost_usd) ?? "—", align: "end", className: "hidden md:table-cell tabular-nums" },
  ];

  const editing = edit.value;
  const editedCase = editing && editing !== "new" && editing !== "suite" ? suite?.cases.find((c) => c.id === editing) ?? null : null;
  const icon = (Icon: typeof Play) => <Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />;

  if (error && !suite) {
    return (
      <div role="alert" className={alertClass} data-testid="eval-suite-load-error">
        {error}{" "}
        <Button size="sm" variant="ghost" onClick={load}>Retry</Button>{" "}
        <Link href={routes.agentTests(agent.id)} className="underline underline-offset-4">All suites</Link>
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-6" data-testid="eval-suite-view">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link href={routes.agentTests(agent.id)} data-testid="eval-suite-back">
            {icon(ArrowLeft)}
            Suites
          </Link>
        </Button>
        <div className="min-w-0 flex-1">
          <h2 className={`${sectionTitleClass} truncate`} data-testid="eval-suite-title">{suite?.name ?? "Loading suite…"}</h2>
          {suite?.description ? <p className="break-words text-label text-muted-foreground">{suite.description}</p> : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => setRunning(true)} disabled={!suite || archived || suite.cases.length === 0} data-testid="eval-suite-run">
            {icon(Play)}
            Run
          </Button>
          <Button onClick={() => fileRef.current?.click()} disabled={!suite || archived || importing} aria-describedby="eval-import-hint" title="A pydantic_evals YAML or JSON file, at most 1 MiB" data-testid="eval-suite-import">
            {icon(Upload)}
            {importing ? "Importing…" : "Import"}
          </Button>
          <input ref={fileRef} type="file" accept=".yaml,.yml,.json,application/json,application/yaml,text/yaml" className="sr-only" tabIndex={-1} aria-label="Suite file to import" onChange={(e) => void onImportFile(e)} data-testid="eval-suite-import-file" />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button disabled={!suite} data-testid="eval-suite-export">
                {icon(Download)}
                Export
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => void onExport("yaml")} data-testid="eval-suite-export-yaml">YAML (pydantic_evals)</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => void onExport("json")} data-testid="eval-suite-export-json">JSON (pydantic_evals)</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <IconButton label="More suite actions" icon={icon(MoreHorizontal)} disabled={!suite} data-testid="eval-suite-more" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => edit.open("suite")} data-testid="eval-suite-edit">Edit suite</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => void setArchived(!archived)} data-testid="eval-suite-archive">{archived ? "Restore suite" : "Archive suite"}</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setConfirm("delete")} destructive data-testid="eval-suite-delete">Delete suite</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <p id="eval-import-hint" className={hintClass} data-testid="eval-import-hint">
        Import takes a pydantic_evals dataset file (YAML or JSON, at most 1 MiB); cases are matched by name.
      </p>
      {archived ? <p className={hintClass} data-testid="eval-suite-archived">This suite is archived: it can be viewed and exported, not edited or run.</p> : null}
      {actionError ? <p role="alert" className={alertClass} data-testid="eval-suite-action-error">{actionError}</p> : null}
      {importError ? <p role="alert" className={alertClass} data-testid="eval-suite-import-error">Import failed: {importError}</p> : null}

      <section className="min-w-0 space-y-2" aria-labelledby="eval-evaluators-title">
        <div className="flex min-w-0 items-center gap-2">
          <h3 id="eval-evaluators-title" className={`${sectionTitleClass} min-w-0 flex-1`}>Evaluators</h3>
          <Button size="sm" variant="ghost" onClick={() => edit.open("suite")} disabled={!suite || archived} data-testid="eval-evaluators-edit">
            {icon(Pencil)}
            Edit
          </Button>
        </div>
        {suite && suite.evaluators.length === 0 ? (
          <p className={hintClass} data-testid="eval-evaluators-empty">No suite evaluators: cases pass unless their own evaluators fail. Add one with Edit.</p>
        ) : (
          <ul className="grid gap-2 [grid-template-columns:repeat(auto-fit,minmax(min(100%,16rem),1fr))]" data-testid="eval-evaluators">
            {(suite?.evaluators ?? []).map((e, i) => (
              <li key={`${e.kind}-${i}`} className={`${cardClass} p-3`} data-testid="eval-evaluator" data-kind={e.kind}>
                <p className="truncate text-label font-medium text-foreground">{evaluatorLabel(e)}</p>
                <p className="break-words text-caption text-muted-foreground">{evaluatorSummary(e)}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="min-w-0 space-y-2" aria-labelledby="eval-cases-title">
        <div className="flex min-w-0 items-center gap-2">
          <h3 id="eval-cases-title" className={`${sectionTitleClass} min-w-0 flex-1`}>Cases</h3>
          <Button size="sm" onClick={() => edit.open("new")} disabled={!canAddCase} data-testid="eval-case-add">
            {icon(Plus)}
            Add case
          </Button>
        </div>
        <DataList<EvalCase>
          label="Cases"
          rows={suite?.cases ?? []}
          columns={caseColumns}
          getRowId={(c) => c.id}
          onRowOpen={(c) => edit.open(c.id)}
          getRowLabel={(c) => `Case ${c.name}`}
          getRowTestId={() => "eval-case-row"}
          selectedRowId={editedCase?.id ?? null}
          loading={suite === null}
          searchPlaceholder="Search cases"
          searchShortcut={false}
          filterRow={(c, q) => `${c.name}\n${c.input}`.toLowerCase().includes(q.toLowerCase())}
          empty={{
            title: "No cases yet",
            description: "Add a case: an input, and optionally the output you expect. Or import a pydantic_evals dataset file.",
            action: canAddCase ? <Button size="sm" variant="primary" onClick={() => edit.open("new")}>Add case</Button> : undefined,
          }}
          testId="eval-cases"
        />
      </section>

      <section className="min-w-0 space-y-2" aria-labelledby="eval-history-title">
        <h3 id="eval-history-title" className={sectionTitleClass}>Run history</h3>
        {suiteRuns.length >= 2 ? (
          <div className="flex min-w-0 flex-wrap items-end gap-2" data-testid="eval-history-compare">
            <div className="min-w-0 flex-1 space-y-1 sm:max-w-xs">
              <label htmlFor="eval-compare-base" className={labelClass}>Base</label>
              <select id="eval-compare-base" className={fieldClass} value={base} onChange={(e) => setBase(e.target.value)} data-testid="eval-compare-base">
                <option value="">Choose a run</option>
                {suiteRuns.map((r) => (
                  <option key={r.run_id} value={r.run_id}>{runTargetLabel(r)} · {new Date(r.created_at).toLocaleString()}</option>
                ))}
              </select>
            </div>
            <div className="min-w-0 flex-1 space-y-1 sm:max-w-xs">
              <label htmlFor="eval-compare-target" className={labelClass}>Target</label>
              <select id="eval-compare-target" className={fieldClass} value={target} onChange={(e) => setTarget(e.target.value)} data-testid="eval-compare-target">
                <option value="">Choose a run</option>
                {suiteRuns.map((r) => (
                  <option key={r.run_id} value={r.run_id}>{runTargetLabel(r)} · {new Date(r.created_at).toLocaleString()}</option>
                ))}
              </select>
            </div>
            <Button
              disabled={!base || !target || base === target}
              onClick={() => router.push(routes.agentTests(agent.id, { suite: suiteId, compare: runCompareParam(base, target) }))}
              data-testid="eval-compare-open"
            >
              Compare
            </Button>
          </div>
        ) : null}
        <DataList<EvalRun>
          label="Eval runs"
          rows={suiteRuns}
          columns={runColumns}
          getRowId={(r) => r.run_id}
          getRowHref={(r) => routes.agentTests(agent.id, { suite: suiteId, run: r.run_id })}
          getRowLabel={(r) => `Eval run ${runTargetLabel(r)} ${new Date(r.created_at).toLocaleString()}`}
          getRowTestId={() => "eval-history-row"}
          loading={runs === null && !runsError}
          error={runsError}
          onRetry={reloadRuns}
          empty={{ title: "No runs yet", description: "Run the suite against a published version or the draft to see results here." }}
          testId="eval-history"
        />
      </section>

      {running && suite ? (
        <RunDialog
          agent={agent}
          versions={versions}
          suite={suite}
          hasMemory={hasMemory}
          onClose={() => setRunning(false)}
          onStarted={(started) => {
            setRunning(false);
            rememberCaseCount(started.run_id, started.cases);
            reloadRuns();
            if (started.warnings.length > 0) toast.warning(`${started.warnings.length} tool(s) with side effects run for real`);
            router.push(routes.agentTests(agent.id, { suite: suiteId, run: started.run_id }));
          }}
        />
      ) : null}

      {suite && editing === "suite" ? (
        <SuiteSheet
          suite={suite}
          onClose={edit.close}
          onSaved={(saved) => {
            setSuite((s) => (s ? { ...s, ...saved } : s));
            toast.success("Suite saved");
            edit.close();
          }}
        />
      ) : null}
      {suite && (editing === "new" || editedCase) ? (
        <CaseSheet
          key={editing ?? ""}
          suite={suite}
          record={editing === "new" ? null : editedCase}
          onClose={edit.close}
          onSaved={(saved) => {
            setSuite((s) => {
              if (!s) return s;
              const exists = s.cases.some((c) => c.id === saved.id);
              const cases = exists ? s.cases.map((c) => (c.id === saved.id ? saved : c)) : [...s.cases, saved];
              return { ...s, cases: cases.sort((a, b) => a.position - b.position), case_count: cases.length };
            });
            toast.success(editing === "new" ? "Case added" : "Case saved");
            edit.close();
          }}
          onDeleted={(caseId) => {
            setSuite((s) => (s ? { ...s, cases: s.cases.filter((c) => c.id !== caseId), case_count: s.case_count - 1 } : s));
            toast.success("Case deleted");
            edit.close();
          }}
        />
      ) : null}

      {confirm === "delete" && suite ? (
        <ConfirmDialog
          title={`Delete suite ${suite.name}?`}
          description="Its cases are deleted. Past eval runs and their results stay."
          confirmLabel="Delete suite"
          destructive
          onConfirm={() => void onDelete()}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
    </div>
  );
}
