// Loop-milestone workflow specs (LP.6), mirroring LP.1's schema test fixtures
// (packages/shared-schemas/tests/test_workflow_loops.py).
import type { WorkflowSpec } from "@/lib/workflows";
import { AGENT } from "./workflowFixtures";

const ref = (path: string) => ({ $ref: path });

/** fetch → each (foreach over tickets: triage → update, 4 at a time) → report. */
export const foreachSpec: WorkflowSpec = {
  spec_version: 1,
  name: "Ticket triage",
  input_schema: { type: "object" },
  start: "fetch",
  steps: [
    { id: "fetch", type: "tool", tool_id: "clock.now", input: {}, next: "each" },
    {
      id: "each",
      type: "foreach",
      items: ref("steps.fetch.output.tickets"),
      max_items: 50,
      concurrency: 4,
      body: {
        start: "triage",
        steps: [
          { id: "triage", type: "agent", agent_id: AGENT, input: { t: ref("loop.each.item") }, next: "update" },
          { id: "update", type: "tool", tool_id: "clock.now", input: { r: ref("steps.triage.output") }, next: null },
        ],
      },
      next: "report",
    },
    { id: "report", type: "tool", tool_id: "clock.now", input: { all: ref("steps.each.output") }, next: null },
  ],
};

/** A polling loop: check until its output says done, with carried state. */
export const untilSpec: WorkflowSpec = {
  spec_version: 1,
  name: "Poll",
  input_schema: { type: "object" },
  start: "poll",
  steps: [
    {
      id: "poll",
      type: "loop",
      until: { op: "eq", left: ref("steps.check.output.done"), right: true },
      max_iterations: 10,
      state: { n: 0 },
      carry: { n: ref("steps.check.output.n") },
      body: { start: "check", steps: [{ id: "check", type: "tool", tool_id: "clock.now", input: { s: ref("loop.poll.state.n") }, next: null }] },
      next: null,
    },
  ],
};

/** Depth 2: a foreach whose body holds a while loop, then a branch over a combinator. */
export const nestedSpec: WorkflowSpec = {
  spec_version: 1,
  name: "Nested",
  input_schema: { type: "object" },
  start: "fetch",
  steps: [
    { id: "fetch", type: "tool", tool_id: "clock.now", input: {}, next: "outer" },
    {
      id: "outer",
      type: "foreach",
      items: ref("steps.fetch.output.tickets"),
      max_items: 20,
      on_item_error: "collect",
      body: {
        start: "inner",
        steps: [
          {
            id: "inner",
            type: "loop",
            while: { all: [{ op: "lt", left: ref("loop.inner.index"), right: 3 }, { not: { op: "exists", left: ref("loop.inner.state.stop") } }] },
            max_iterations: 5,
            on_max_iterations: "exit",
            state: { k: ref("loop.outer.item") },
            body: {
              start: "leaf",
              steps: [{ id: "leaf", type: "tool", tool_id: "clock.now", input: { it: ref("loop.outer.item") }, next: null }],
            },
            next: "tally",
          },
          { id: "tally", type: "tool", tool_id: "clock.now", input: {}, next: null },
        ],
      },
      next: "route",
    },
    {
      id: "route",
      type: "branch",
      cases: [{ when: { any: [{ op: "eq", left: ref("input.a"), right: 1 }, { op: "exists", left: ref("input.b") }] }, next: "done" }],
      default: "done",
    },
    { id: "done", type: "agent", agent_id: AGENT, input: {}, next: null },
  ],
};

/** Refine: loop until the evaluate step passes. */
export const refineSpec: WorkflowSpec = {
  spec_version: 1,
  name: "Refine",
  input_schema: { type: "object" },
  start: "refine",
  steps: [
    {
      id: "refine",
      type: "loop",
      until: { op: "eq", left: ref("steps.judge.output.passed"), right: true },
      max_iterations: 3,
      on_max_iterations: "exit",
      state: { feedback: "" },
      carry: { feedback: ref("steps.judge.output.reason") },
      body: {
        start: "write",
        steps: [
          { id: "write", type: "agent", agent_id: AGENT, input: { fb: ref("loop.refine.state.feedback") }, next: "judge" },
          {
            id: "judge",
            type: "evaluate",
            value: ref("steps.write.output.draft"),
            evaluators: [{ kind: "llm_judge", rubric: "good?", model: { provider: "test", name: "judge" } }],
            next: null,
          },
        ],
      },
      next: null,
    },
  ],
};
