import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { WorkflowForm } from "./WorkflowForm";
import {
  compilePreview,
  placeholdersFor,
  problemsFor,
  ArchitectApiError,
  rowsFromSteps,
  stepsFromRows,
  type WfSpec,
} from "@/lib/architect-workflow";

const options = {
  tools: [{ id: "clock.now", title: "Clock", description: "The time", requires_approval: false }],
  agents: [
    { id: "writer", name: "Writer" },
    { id: "reviewer", name: "Reviewer" },
  ],
};
const agents = [
  { id: "writer", name: "Writer", versions: 1, archived: false },
  { id: "reviewer", name: "Reviewer", versions: 2, archived: false },
];

const spec: WfSpec = {
  name: "Weekly report",
  id: "weekly-report",
  description: "Writes the report.",
  input_description: "The topic.",
  steps: [
    { kind: "agent", id: "draft", agent: "writer", prompt: "Write about {{input}}" },
    {
      kind: "parallel",
      id: "checks",
      branches: [
        { kind: "agent", id: "facts", agent: "reviewer", prompt: "{{previous}}" },
        { kind: "agent", id: "tone", agent: "reviewer", prompt: "{{steps.draft}}" },
      ],
    },
    { kind: "tool", id: "stamp", tool: "clock.now", params: { zone: "{{input}}" } },
    { kind: "agent", id: "final", agent: "writer", prompt: "{{steps.facts}} {{steps.tone}}" },
  ],
};

type Call = { url: string; method: string; body: Record<string, unknown> | null };
let calls: Call[];
let responder: (call: Call) => { status: number; body: unknown };

function json(status: number, body: unknown) {
  return { status, body };
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer test";
  // React Flow reads the viewport transform through DOMMatrixReadOnly, which jsdom lacks.
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
  calls = [];
  responder = () => json(200, {});
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call: Call = { url: String(url), method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null };
      calls.push(call);
      const { status, body } = responder(call);
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const form = (props: Partial<React.ComponentProps<typeof WorkflowForm>> = {}) =>
  render(
    <TooltipProvider>
      <div style={{ width: 900, height: 900 }}>
        <WorkflowForm options={options} agents={agents} onSaved={vi.fn()} {...props} />
      </div>
    </TooltipProvider>,
  );

describe("the restricted format", () => {
  it("round-trips steps through the editable rows", () => {
    const { steps, errors } = stepsFromRows(rowsFromSteps(spec.steps));
    expect(errors).toEqual({});
    expect(steps).toEqual(spec.steps);
  });

  it("reports tool params that are not a JSON object on their row", () => {
    const rows = rowsFromSteps([{ kind: "tool", id: "t", tool: "clock.now", params: {} }]);
    if (rows[0].kind !== "tool") throw new Error("tool row");
    rows[0].paramsText = "[1]";
    expect(stepsFromRows(rows).errors[rows[0].key]).toMatch(/JSON object/);
    rows[0].paramsText = "{oops";
    expect(stepsFromRows(rows).errors[rows[0].key]).toMatch(/not valid JSON/);
  });

  it("compiles the preview like the API: a parallel step joins at the next step, and cannot be last", () => {
    const { spec: preview, problems } = compilePreview(spec);
    expect(problems).toEqual([]);
    const byId = Object.fromEntries(preview.steps.map((s) => [s.id, s]));
    expect(preview.start).toBe("draft");
    expect(byId.draft).toMatchObject({ type: "agent", next: "checks" });
    expect(byId.checks).toMatchObject({ type: "parallel", branches: ["facts", "tone"], join: "stamp" });
    expect(byId.facts).toMatchObject({ type: "agent", next: null });
    expect(byId.stamp).toMatchObject({ type: "tool", tool_id: "clock.now", next: "final" });
    expect(byId.final).toMatchObject({ next: null });
    const last = compilePreview({ ...spec, steps: spec.steps.slice(0, 2) });
    expect(last.problems).toEqual([expect.objectContaining({ stepId: "checks" })]);
  });

  it("offers the placeholders a step may use", () => {
    expect(placeholdersFor(spec.steps, 0)).toEqual(["{{input}}"]);
    expect(placeholdersFor(spec.steps, 1)).toEqual(["{{input}}", "{{previous}}", "{{steps.draft}}"]);
    // after a parallel step: no {{previous}}, the branches by id
    expect(placeholdersFor(spec.steps, 2)).toEqual(["{{input}}", "{{steps.draft}}", "{{steps.facts}}", "{{steps.tone}}"]);
    expect(placeholdersFor(spec.steps, 3)).toEqual(["{{input}}", "{{previous}}", "{{steps.draft}}", "{{steps.facts}}", "{{steps.tone}}", "{{steps.stamp}}"]);
  });

  it("sorts a refusal onto the steps that own it", () => {
    const err = (code: string, message: string) => new ArchitectApiError(422, message, code, { detail: message, code });
    expect(problemsFor(err("agent_not_allowed", "agent_not_allowed: reviewer"), spec).byStep).toEqual({
      facts: [expect.stringContaining("reviewer")],
      tone: [expect.stringContaining("reviewer")],
    });
    const unpublished = new ArchitectApiError(422, "step 'facts': agent 'x' has no published version", "agent_not_published", {
      detail: "x", code: "agent_not_published", agents: [{ step: "facts", id: "reviewer", name: "Reviewer" }],
    });
    expect(problemsFor(unpublished, spec).byStep).toEqual({
      facts: [expect.stringContaining("no published version yet")],
      tone: [expect.stringContaining("no published version yet")],
    });
    expect(problemsFor(err("tool_not_allowed", "tool_not_allowed: clock.now"), spec).byStep).toHaveProperty("stamp");
    expect(problemsFor(err("unknown_placeholder", "unknown_placeholder: {{steps.tone}} (no earlier step 'tone')"), spec).byStep).toHaveProperty("final");
    expect(problemsFor(err("placeholder_not_whole_value", "placeholder_not_whole_value: param 'zone' must be exactly one placeholder"), spec).byStep).toHaveProperty("stamp");
    expect(problemsFor(err("workflow_invalid", "workflow_invalid: nope"), spec)).toEqual({ byStep: {}, general: ["workflow_invalid: nope"] });
    const shape = new ArchitectApiError(422, "x", null, { detail: [{ loc: ["body", "spec", "steps", 1, "parallel", "branches", 0, "agent"], msg: "bad slug" }] });
    expect(problemsFor(shape, spec).byStep).toEqual({ facts: ["bad slug"] });
  });
});

describe("WorkflowForm", () => {
  it("shows the steps as rows and previews the compiled graph", () => {
    form({ initial: { spec, version: 1 } });
    expect(screen.getByRole("group", { name: /Step 1/ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Step 2/ })).toHaveTextContent("Parallel");
    const preview = screen.getByTestId("workflow-preview");
    for (const id of ["draft", "checks", "facts", "tone", "stamp", "final"]) {
      expect(within(preview).getByTestId(`workflow-node-${id}`)).toBeInTheDocument();
    }
  });

  it("the preview follows the rows", () => {
    form({ initial: { spec, version: 1 } });
    fireEvent.click(screen.getByRole("button", { name: "Add agent step" }));
    expect(within(screen.getByTestId("workflow-preview")).getByTestId("workflow-node-step_1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove step 1" }));
    expect(within(screen.getByTestId("workflow-preview")).queryByTestId("workflow-node-draft")).toBeNull();
  });

  it("inserts a placeholder into a prompt", () => {
    form({ initial: { spec, version: 1 } });
    const step2 = screen.getByRole("group", { name: /Step 2/ });
    const prompt = within(step2).getAllByLabelText("Prompt")[0] as HTMLTextAreaElement;
    fireEvent.change(prompt, { target: { value: "Check: " } });
    const picker = within(step2).getAllByLabelText("Insert placeholder")[0];
    fireEvent.change(picker, { target: { value: "{{steps.draft}}" } });
    expect(prompt.value).toBe("Check: {{steps.draft}}");
  });

  it("Save and go live posts publish: true with the version it last saw, then shows the warnings", async () => {
    const onSaved = vi.fn();
    responder = () => json(200, { id: "weekly-report", workflow_id: "wf-1", version: 2, published: true, warnings: [{ code: "unpriced_model", path: "model", message: "No price for this model." }] });
    form({ initial: { spec, version: 1 }, onSaved });
    fireEvent.click(screen.getByRole("button", { name: "Save and go live" }));
    await screen.findByTestId("publish-warnings");
    expect(screen.getByText("No price for this model.")).toBeInTheDocument();
    const post = calls.find((c) => c.url.endsWith("/api/v1/architect/workflows"))!;
    expect(post.method).toBe("POST");
    expect(post.body).toMatchObject({ publish: true, expected_version: 1, spec: { name: "Weekly report", id: "weekly-report" } });
    expect((post.body!.spec as { steps: unknown[] }).steps).toHaveLength(4);
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ workflow_id: "wf-1", version: 2, published: true }), expect.objectContaining({ name: "Weekly report" }));
  });

  it("Save draft posts publish: false and offers no schedule", async () => {
    responder = () => json(200, { id: "weekly-report", workflow_id: "wf-1", version: null, published: false, warnings: [] });
    form({ initial: { spec, version: null } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await screen.findByText(/Saved as a draft/);
    const post = calls.find((c) => c.url.endsWith("/api/v1/architect/workflows"))!;
    expect(post.body).toMatchObject({ publish: false });
    expect(post.body).not.toHaveProperty("expected_version");
    expect(screen.queryByRole("group", { name: "Schedule" })).toBeNull();
  });

  it("after a publish, a schedule is created through the schedules route for that workflow", async () => {
    responder = (call) =>
      call.url.endsWith("/schedules")
        ? json(201, { id: "s1", workflow_id: "wf-1", name: "Mornings", cron: "0 9 * * *", timezone: "UTC", input: { input: "sales" }, active: true, next_fire_at: "2026-11-18T09:00:00Z" })
        : json(200, { id: "weekly-report", workflow_id: "wf-1", version: 2, published: true, warnings: [] });
    form({ initial: { spec, version: 1 } });
    fireEvent.click(screen.getByRole("button", { name: "Save and go live" }));
    const block = await screen.findByRole("group", { name: "Schedule" });
    fireEvent.change(within(block).getByLabelText("Name"), { target: { value: "Mornings" } });
    fireEvent.change(within(block).getByLabelText("Cron"), { target: { value: "0 9 * * *" } });
    fireEvent.change(within(block).getByLabelText("Timezone"), { target: { value: "UTC" } });
    fireEvent.change(within(block).getByLabelText("Input"), { target: { value: "sales" } });
    fireEvent.click(within(block).getByRole("button", { name: "Create schedule" }));
    await within(block).findByText(/Schedule "Mornings" created/);
    const post = calls.find((c) => c.url.endsWith("/api/v1/workflows/wf-1/schedules"))!;
    expect(post.method).toBe("POST");
    expect(post.body).toEqual({ name: "Mornings", cron: "0 9 * * *", timezone: "UTC", input: { input: "sales" } });
  });

  it("puts a 422 on the step row that owns it", async () => {
    responder = () => json(422, { detail: "agent_not_allowed: reviewer", code: "agent_not_allowed" });
    form({ initial: { spec, version: 1 } });
    fireEvent.click(screen.getByRole("button", { name: "Save and go live" }));
    const step2 = await screen.findByRole("group", { name: /Step 2/ });
    await within(step2).findAllByText(/"reviewer" is not available/);
    expect(within(screen.getByRole("group", { name: /Step 1/ })).queryByRole("alert")).toBeNull();
    expect(within(screen.getByRole("group", { name: /Step 4/ })).queryByRole("alert")).toBeNull();
  });

  it("a stale version shows the 409 state and can retry on the latest", async () => {
    responder = () => json(409, { detail: "The workflow 'weekly-report' is at version 5, not 1; reload it.", code: "version_mismatch", latest_version: 5 });
    form({ initial: { spec, version: 1 } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    const conflict = await screen.findByTestId("workflow-version-conflict");
    expect(conflict).toHaveTextContent("version 5");
    responder = () => json(200, { id: "weekly-report", workflow_id: "wf-1", version: 5, published: false, warnings: [] });
    fireEvent.click(within(conflict).getByRole("button", { name: "Save over version 5" }));
    await screen.findByText(/Saved as a draft/);
    expect(calls[calls.length - 1].body).toMatchObject({ expected_version: 5 });
    expect(screen.queryByTestId("workflow-version-conflict")).toBeNull();
  });

  it("Fill in the form runs the drafter and fills the rows, listing assumptions and what was dropped", async () => {
    responder = () =>
      json(200, {
        spec: { name: "Greeting flow", id: null, description: "", input_description: "A name.", steps: [{ kind: "agent", id: "hi", agent: "writer", prompt: "Greet {{input}}" }] },
        assumptions: ["One step is enough."],
        dropped: [{ kind: "tool", id: "shell.exec" }],
        run_id: "r1",
        model: "test:echo",
      });
    form();
    fireEvent.change(screen.getByLabelText("Describe the workflow"), { target: { value: "greet people" } });
    fireEvent.click(screen.getByRole("button", { name: "Fill in the form" }));
    await screen.findByText("One step is enough.");
    expect(screen.getByText(/shell\.exec/)).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("Greeting flow");
    expect(within(screen.getByRole("group", { name: /Step 1/ })).getByLabelText("Step id")).toHaveValue("hi");
    const draft = calls.find((c) => c.url.endsWith("/api/v1/architect/draft-workflow"))!;
    expect(draft.body).toEqual({ description: "greet people" });
  });

  it("shows a draft_invalid answer's problems and keeps the form", async () => {
    responder = () => json(422, { detail: "The drafter's answer is not a valid workflow definition.", code: "draft_invalid", raw: "nope", problems: ["steps: too short"] });
    form({ initial: { spec, version: 1 } });
    fireEvent.change(screen.getByLabelText("Describe the workflow"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Fill in the form" }));
    await screen.findByText("steps: too short");
    expect(screen.getByLabelText("Name")).toHaveValue("Weekly report");
  });

  it("an invalid or failed workflow drafter answer is the readable drafter alert with the raw answer collapsed", async () => {
    responder = () => json(422, { detail: "The drafter's answer is not a valid workflow definition.", code: "draft_invalid", raw: "not json at all", problems: ["steps: too short"] });
    form({ initial: { spec, version: 1 } });
    fireEvent.change(screen.getByLabelText("Describe the workflow"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Fill in the form" }));
    const alert = await screen.findByTestId("architect-drafter-failure");
    expect(alert).toHaveTextContent("The drafter's model did not produce the form. Choose another model.");
    expect((alert.querySelector("details") as HTMLDetailsElement).open).toBe(false);
    expect(alert.querySelector("details")).toHaveTextContent("not json at all");
    responder = () => json(502, { detail: "The drafter run did not finish.", code: "draft_failed", run_id: "r", error: "boom" });
    fireEvent.click(screen.getByRole("button", { name: "Fill in the form" }));
    await waitFor(() => expect(screen.getByTestId("architect-drafter-failure").querySelector("details")).toHaveTextContent("boom"));
  });

  it("will not save without a name or a step", async () => {
    form();
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText(/Give the workflow a name/)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });
});
