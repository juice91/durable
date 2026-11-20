import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  collectQueries,
  collectRenderedIds,
  findDeadQueries,
  isRenderedPrefix,
  type SourceFile,
} from "./testidInventory";

const srcDir = path.resolve(__dirname, "..");

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (/\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

const isTest = (file: string) => /\.test\.tsx?$/.test(file);
const read = (file: string): SourceFile => ({ path: path.relative(srcDir, file), text: fs.readFileSync(file, "utf-8") });
const files = walk(srcDir);
const self = (f: string) => f.endsWith("testidInventory.ts") || f.endsWith("testid-inventory.test.ts");
// Rendered ids come from non-test sources. A test file may render its own harness markup; those ids
// count only for queries in that same file (localIds), never for other tests.
const sources = files.filter((f) => !self(f) && !isTest(f)).map(read);
const tests = files.filter((f) => isTest(f) && !self(f)).map(read);

/**
 * Dynamic test-id prefixes: ids built from data at render time. A query matches one of these by
 * prefix only. Each entry says where the id is rendered and why it cannot be a literal.
 */
export const DYNAMIC_PREFIXES: Record<string, string> = {
  "kpi-": "KpiCard testId={`kpi-${id}`} (MetricsKpis, the Metrics Overview KPI row), plus its -value and -delta children: one card per KPI id",
  "chart-": "TimeSeriesChart data-testid={`chart-${title}`}: one per chart title",
  "breakdown-row-": "BreakdownTable rows: breakdown-row-<agent|model|provider>",
  "breakdown-change-": "BreakdownTable p99/change cell per breakdown and key: breakdown-change-<agent|model|provider>-<key> (Task 18)",
  "phase-": "ProcessorList row per processor phase id: phase-<phase> (Task 11)",
  "workflow-row-": "Workflows list row per workflow id",
  "schedule-last-run-": "WorkflowSchedules last-run cell per schedule id (PR 73)",
  "workflow-step-": "WorkflowGraph node per step id",
  "workflow-step-ai-note-": "WorkflowStepList AI note (the cached explanation) per step id",
  "step-row-text-": "WorkflowStepList description text per step id",
  "workflow-node-": "WorkflowRunView graph node per step id",
  "workflow-loop-badge-": "WorkflowGraph loop node's ×n badge per loop step id (LP.6)",
  "workflow-iteration-pager-": "LoopIterations pager and its parts per loop step id (LP.12)",
  "workflow-loop-iterations-": "LoopIterations table per loop step id (LP.12)",
  "workflow-loop-iteration-": "LoopIterations table row per loop step id and iteration index (LP.12)",
  "workflow-timeline-iteration-": "TimelineDock iteration row per loop step id and iteration index (LP.12)",
  "workflow-loop-toggle-": "WorkflowStepList per-loop collapse button per loop step id (LP.6)",
  "workflow-loop-summary-": "WorkflowGraph loop group's condition summary per loop step id (LP.6)",
  "workflow-run-row-": "WorkflowRunHistory row per run id",
  "workflow-timeline-row-": "TimelineDock row per step id (SL.14): workflow-timeline-row-<stepId>",
  "approval-": "ApprovalCard per step id: approval-, approval-prompt-, approval-error-, approval-deadline-<stepId>",
  "nav-badge-": "Sidebar NavLink badge per nav item id: nav-badge-<id> (count) and nav-badge-dot-<id> (rail dot), AC.15",
  "processors-": "ProcessorsEditor section per phase (input or output)",
  "field-error-": "Agent editor field errors keyed by the spec field path",
  "console-": "ProcessorTestConsole idPrefix={`console-${step.key}`}: one form per processor step",
  "processor-input-": "ProcessorsEditor input rows: processor-input-<index>, plus -error",
  "processor-output-": "ProcessorsEditor output rows: processor-output-<index>, plus -error",
  "context-field-": "ContextFieldsEditor rows: context-field-<index>, plus -problems",
  "payload-": "PayloadBlock id from its label: payload-input, payload-output, plus -truncated",
};

/** Ids rendered by third-party components, which no file under src/ contains. */
export const EXTERNAL_PREFIXES: Record<string, string> = {
  "rf__edge-": "@xyflow/react renders each graph edge as data-testid=\"rf__edge-<edge id>\"",
};

describe("test-id inventory", () => {
  const rendered = collectRenderedIds(sources);
  const queries = collectQueries(tests);
  const localIds = new Map(tests.map((f) => [f.path, collectRenderedIds([f])] as const));

  it("finds the test ids the suite queries", () => {
    // Guards the collectors themselves: a regex change that matched nothing would pass vacuously.
    expect(rendered.exact.size).toBeGreaterThan(100);
    expect(queries.length).toBeGreaterThan(500);
  });

  it("every queried test id is rendered by some source file", () => {
    const dead = findDeadQueries(queries, rendered, Object.keys(DYNAMIC_PREFIXES), Object.keys(EXTERNAL_PREFIXES), localIds);
    expect(dead.map((d) => `${d.file}:${d.line} ${d.kind} "${d.value}": ${d.reason}`)).toEqual([]);
  });

  it("every allowlisted dynamic prefix is still rendered (no stale entries)", () => {
    const stale = Object.keys(DYNAMIC_PREFIXES).filter((p) => !isRenderedPrefix(p, rendered));
    expect(stale).toEqual([]);
  });
});

describe("test-id inventory fixtures", () => {
  const source: SourceFile = {
    path: "components/Fixture.tsx",
    text: [
      '<div data-testid="alive" />',
      "<div data-testid={busy ? 'busy-a' : \"busy-b\"} />",
      "<li data-testid={`row-${item.id}`} />",
      '<Card testId="card-total" />',
      "const links = [{ testId: \"link-run\" }];",
      'document.querySelector(\'[data-testid="selector-only"]\');',
      "type P = { testId: string };",
      "const id = `payload-${label}`;",
      "<div data-testid={id} />",
      "<p data-testid={`${id}-truncated`} />",
      '<DataList getRowTestId={() => "list-row"} />',
    ].join("\n"),
  };
  const rendered = collectRenderedIds([source]);
  const test = (text: string): SourceFile => ({ path: "components/Fixture.test.tsx", text });

  it("collects literal, ternary, prop and key ids, and template heads as prefixes", () => {
    expect([...rendered.exact].sort()).toEqual(["alive", "busy-a", "busy-b", "card-total", "link-run", "list-row"]);
    expect([...rendered.prefixes.keys()].sort()).toEqual(["payload-", "row-"]);
  });

  it("a narrow allowlist entry under a broader rendered head matches only its own ids", () => {
    const queries = collectQueries([test('screen.getByTestId("row-a-1"); screen.getByTestId("row-b-1");')]);
    expect(isRenderedPrefix("row-a-", rendered)).toBe(true);
    expect(isRenderedPrefix("other-", rendered)).toBe(false);
    expect(findDeadQueries(queries, rendered, ["row-a-"]).map((d) => d.value)).toEqual(["row-b-1"]);
  });

  it("fails on a deliberately dead id", () => {
    const queries = collectQueries([test('screen.getByTestId("alive"); screen.queryByTestId("dead-id");')]);
    expect(findDeadQueries(queries, rendered, []).map((d) => d.value)).toEqual(["dead-id"]);
  });

  it("matches a dynamic id only through an allowlisted prefix", () => {
    const queries = collectQueries([test('getByTestId("row-42"); getByTestId(`row-${id}`);')]);
    expect(findDeadQueries(queries, rendered, []).map((d) => d.value)).toEqual(["row-42", "row-"]);
    expect(findDeadQueries(queries, rendered, ["row-"])).toEqual([]);
  });

  it("checks regex queries and selectors", () => {
    const queries = collectQueries([
      test('getAllByTestId(/^busy-/); getByTestId(/^nothing-/); el.querySelector("[data-testid=\'gone\']");'),
    ]);
    expect(findDeadQueries(queries, rendered, []).map((d) => `${d.kind}:${d.value}`)).toEqual([
      "regex:^nothing-",
      "exact:gone",
    ]);
  });

  it("allows third-party prefixes without a source match", () => {
    const queries = collectQueries([test("el.querySelectorAll('[data-testid^=\"lib__edge-\"]');")]);
    expect(findDeadQueries(queries, rendered, [])).toHaveLength(1);
    expect(findDeadQueries(queries, rendered, [], ["lib__edge-"])).toEqual([]);
  });

  it("counts ids a test file renders itself only for that file's queries", () => {
    const harness = test('render(<div data-testid="harness-only" />); getByTestId("harness-only");');
    const other: SourceFile = { path: "components/Other.test.tsx", text: 'getByTestId("harness-only");' };
    const queries = collectQueries([harness, other]);
    const localIds = new Map([[harness.path, collectRenderedIds([harness])]]);
    expect(findDeadQueries(queries, rendered, [], [], localIds).map((d) => d.file)).toEqual(["components/Other.test.tsx"]);
  });

  it("does not treat a generic `prefix` prop as a test id", () => {
    expect(collectRenderedIds([{ path: "x.tsx", text: '<Input prefix="$" />' }]).exact.size).toBe(0);
  });

  it("skips queries through a variable", () => {
    expect(collectQueries([test("getByTestId(someId);")])).toEqual([]);
  });
});
