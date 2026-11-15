import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import { agentTabOf, routes } from "@/lib/routes";
import { specDiffHref } from "@/components/evals/CompareView";
import {
  blankEvaluatorDraft,
  deltaTone,
  draftToEvaluator,
  draftsToEvaluators,
  evaluatorApiErrors,
  evaluatorToDraft,
  expectedModeOf,
  expectedText,
  failureReason,
  formatRate,
  formatRateDelta,
  formatSigned,
  importFormatOf,
  parseExpected,
  parseMetadata,
  parseModelRef,
  parseRunCompare,
  runCompareParam,
  runTargetLabel,
  type EvalCaseResult,
  type Evaluator,
} from "./evals";

const RUN_A = "11111111-2222-3333-4444-555555555555";
const RUN_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

describe("Tests tab routes (AC.16)", () => {
  it("builds /agents/:id/tests with suite, run and compare in that order", () => {
    expect(routes.agentTests("a1")).toBe("/agents/a1/tests");
    expect(routes.agentTests("a1", { suite: "s1" })).toBe("/agents/a1/tests?suite=s1");
    expect(routes.agentTests("a1", { suite: "s1", run: RUN_A })).toBe(`/agents/a1/tests?suite=s1&run=${RUN_A}`);
    expect(routes.agentTests("a1", { suite: "s1", compare: runCompareParam(RUN_A, RUN_B) })).toBe(`/agents/a1/tests?suite=s1&compare=${RUN_A}..${RUN_B}`);
    expect(routes.agentTab("a1", "tests")).toBe("/agents/a1/tests");
  });

  it("agentTabOf knows the tests tab", () => {
    expect(agentTabOf("/agents/x/tests")).toBe("tests");
    expect(agentTabOf("/agents/x/tests/")).toBe("tests");
    expect(agentTabOf("/agents/x/testsuite")).toBeNull();
  });

  it("parses ?compare= of two different run ids", () => {
    expect(parseRunCompare(`${RUN_A}..${RUN_B}`)).toEqual({ base: RUN_A, target: RUN_B });
    expect(parseRunCompare(`${RUN_A}..${RUN_A}`)).toBeNull();
    expect(parseRunCompare("3..draft")).toBeNull();
    expect(parseRunCompare(null)).toBeNull();
  });

  it("links the spec diff to the editor's ?compare= of the two targets, none for the same target", () => {
    const side = (agent_version: number | null) => ({ agent_version, draft: agent_version === null }) as never;
    expect(specDiffHref("a1", { base: side(3), target: side(4) })).toBe("/agents/a1/editor?compare=3..4");
    expect(specDiffHref("a1", { base: side(3), target: side(null) })).toBe("/agents/a1/editor?compare=3..draft");
    expect(specDiffHref("a1", { base: side(3), target: side(3) })).toBeNull();
    expect(specDiffHref("a1", { base: side(null), target: side(null) })).toBeNull();
  });
});

describe("evaluator drafts", () => {
  const roundTrip = (evaluator: Evaluator) => {
    const out = draftToEvaluator(evaluatorToDraft(evaluator));
    expect(out).toEqual({ evaluator });
  };

  it("round-trips every kind", () => {
    roundTrip({ kind: "equals_expected" });
    roundTrip({ kind: "equals_expected", name: "exact" });
    roundTrip({ kind: "contains", value: "refund" });
    roundTrip({ kind: "contains", case_sensitive: false });
    roundTrip({ kind: "regex", pattern: "^order-\\d+$" });
    roundTrip({ kind: "schema_valid" });
    roundTrip({ kind: "schema_valid", json_schema: { type: "object", properties: { a: { type: "string" } } } });
    roundTrip({ kind: "field_equals", path: "answer.priority", value: "high" });
    roundTrip({ kind: "field_equals", path: "items.0", value: { a: 1 } });
    roundTrip({ kind: "max_duration", seconds: 2.5 });
    roundTrip({ kind: "max_cost", usd: "0.01" });
    roundTrip({ kind: "llm_judge", rubric: "MUST_CONTAIN:ok", model: { provider: "test", name: "judge" }, include_input: true, include_expected: false });
    roundTrip({ kind: "llm_judge", rubric: "r", model: { provider: "openai", name: "gpt-x" }, include_input: false, include_expected: true, pass_threshold: 0.7 });
  });

  it("reports the first problem of a draft", () => {
    const d = (kind: Evaluator["kind"], patch: Partial<ReturnType<typeof blankEvaluatorDraft>> = {}) => ({ ...blankEvaluatorDraft(kind), ...patch });
    expect(draftToEvaluator(d("regex"))).toEqual({ error: "Enter a pattern" });
    expect(draftToEvaluator(d("regex", { pattern: "x".repeat(201) }))).toEqual({ error: "The pattern must be at most 200 characters" });
    expect(draftToEvaluator(d("schema_valid", { jsonSchema: "[1]" }))).toEqual({ error: "The schema must be a JSON object" });
    expect(draftToEvaluator(d("field_equals", { path: "a" , value: "high" }))).toEqual({ error: "The value must be JSON (a string in quotes)" });
    expect(draftToEvaluator(d("max_duration", { seconds: "0" }))).toHaveProperty("error");
    expect(draftToEvaluator(d("max_cost", { usd: "abc" }))).toHaveProperty("error");
    expect(draftToEvaluator(d("llm_judge", { rubric: "r", model: "" }))).toEqual({ error: "Choose a judge model" });
    expect(draftToEvaluator(d("llm_judge", { rubric: " ", model: "test:judge" }))).toEqual({ error: "Enter a rubric" });
    expect(draftToEvaluator(d("llm_judge", { rubric: "r", model: "test:judge", passThreshold: "2" }))).toHaveProperty("error");
  });

  it("refuses two evaluators with the same name, naming the second", () => {
    const a = { ...blankEvaluatorDraft("equals_expected"), name: "x" };
    const b = { ...blankEvaluatorDraft("contains"), name: "x" };
    expect(draftsToEvaluators([a, b])).toEqual({ error: "The name “x” is used twice", index: 1 });
    expect(draftsToEvaluators([a])).toEqual({ evaluators: [{ kind: "equals_expected", name: "x" }] });
  });

  it("parses provider:name model refs at the first colon", () => {
    expect(parseModelRef("test:judge")).toEqual({ provider: "test", name: "judge" });
    expect(parseModelRef("bedrock:anthropic:claude")).toEqual({ provider: "bedrock", name: "anthropic:claude" });
    expect(parseModelRef("judge")).toBeNull();
    expect(parseModelRef("test:")).toBeNull();
  });
});

describe("expected output and metadata", () => {
  it("edits a string as text and anything else as JSON", () => {
    expect(expectedModeOf(null)).toBe("none");
    expect(expectedModeOf("hello")).toBe("text");
    expect(expectedModeOf({ a: 1 })).toBe("json");
    expect(expectedModeOf(3)).toBe("json");
    expect(expectedText({ a: 1 }, "json")).toBe('{\n  "a": 1\n}');
    expect(parseExpected("none", "x")).toEqual({ value: null });
    expect(parseExpected("text", "hi")).toEqual({ value: "hi" });
    expect(parseExpected("json", '{"a": 1}')).toEqual({ value: { a: 1 } });
    expect(parseExpected("json", "{")).toEqual({ error: "Expected output is not valid JSON" });
  });

  it("accepts a flat metadata object only", () => {
    expect(parseMetadata("")).toEqual({ value: {} });
    expect(parseMetadata('{"topic": "billing", "n": 1}')).toEqual({ value: { topic: "billing", n: 1 } });
    expect(parseMetadata("[1]")).toEqual({ error: "Metadata must be a JSON object" });
    expect(parseMetadata('{"a": {"b": 1}}')).toHaveProperty("error");
    const many = Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`k${i}`, i]));
    expect(parseMetadata(JSON.stringify(many))).toEqual({ error: "Metadata may have at most 16 keys" });
  });
});

describe("results and labels", () => {
  const result = (patch: Partial<EvalCaseResult>): EvalCaseResult => ({
    case_id: "c1",
    case_name: "one",
    position: 0,
    child_run_id: null,
    status: "failed",
    output: "x",
    assertions: {},
    scores: {},
    labels: {},
    evaluator_failures: [],
    duration_ms: 10,
    cost_usd: "0.001",
    error_code: null,
    created_at: null,
    ...patch,
  });

  it("says why a case did not pass: agent error, failed assertion with its (judge) reason, evaluator failure", () => {
    expect(failureReason(result({ status: "passed" }))).toBeNull();
    expect(failureReason(result({ status: "error", error_code: "output_invalid" }))).toBe("Agent run failed: output_invalid");
    expect(failureReason(result({ assertions: { equals_expected: { value: true }, llm_judge: { value: false, reason: "No MUST_CONTAIN:ok marker" } } }))).toBe(
      "llm_judge: No MUST_CONTAIN:ok marker",
    );
    expect(failureReason(result({ assertions: { contains: { value: false } } }))).toBe("contains failed");
    expect(failureReason(result({ evaluator_failures: [{ name: "regex", kind: "regex", error: "boom" }] }))).toBe("regex: boom");
  });

  it("formats rates, deltas and targets", () => {
    expect(formatRate(0.8333333)).toBe("83.3%");
    expect(formatRate(null)).toBe("—");
    expect(formatRateDelta(0.125)).toBe("+12.5 pts");
    expect(formatRateDelta(-0.5)).toBe("−50 pts");
    expect(formatRateDelta(0)).toBe("±0 pts");
    expect(formatSigned("-0.0010", "$")).toBe("−$0.0010");
    expect(formatSigned(0.25)).toBe("+0.25");
    expect(formatSigned(null)).toBe("—");
    expect(deltaTone(0.1)).toBe("better");
    expect(deltaTone("0.002", true)).toBe("worse");
    expect(deltaTone(0)).toBe("same");
    expect(runTargetLabel({ version: 3 })).toBe("v3");
    expect(runTargetLabel({ version: null })).toBe("Draft");
    expect(importFormatOf("suite.JSON")).toBe("json");
    expect(importFormatOf("suite.yaml")).toBe("yaml");
  });
});

describe("evaluatorApiErrors", () => {
  it("routes evaluators.N locs to row N (kind tag dropped) and keeps the rest for the banner", () => {
    const error = new ApiError(422, "x", [
      { loc: ["body", "evaluators", 1, "regex", "pattern"], msg: "bad pattern" },
      { loc: ["body", "evaluators", 1, "regex", "name"], msg: "too long" },
      { loc: ["body", "name"], msg: "name taken" },
    ]);
    const split = evaluatorApiErrors(error)!;
    expect([...split.rows.entries()]).toEqual([[1, "pattern: bad pattern; name: too long"]]);
    expect(split.other).toEqual(["name taken"]);
    expect(evaluatorApiErrors(new ApiError(409, "conflict"))).toBeNull();
    expect(evaluatorApiErrors(new Error("x"))).toBeNull();
  });
});

describe("evaluator value types survive a save (pydantic_evals Contains takes any JSON value)", () => {
  const VALUES: unknown[] = ["text", 42, 0.5, true, false, [1, "a"], { a: 1, b: [true] }];

  it("an untouched draft saves every evaluator exactly as stored", () => {
    const stored: Evaluator[] = [
      ...VALUES.map((value) => ({ kind: "contains", value }) as Evaluator),
      { kind: "contains", value: { nested: { x: null } }, case_sensitive: false, name: "obj" },
      ...VALUES.map((value) => ({ kind: "field_equals", path: "a.b", value }) as Evaluator),
      { kind: "max_cost", usd: "1E-7" },
      { kind: "max_cost", usd: 0.01 },
      { kind: "max_duration", seconds: 2 },
      { kind: "schema_valid", json_schema: { type: "object", properties: { n: { type: "integer" } } } },
      { kind: "llm_judge", rubric: "r", model: { provider: "test", name: "judge" }, pass_threshold: 0.5 },
    ];
    for (const evaluator of stored) expect(draftToEvaluator(evaluatorToDraft(evaluator))).toEqual({ evaluator });
  });

  it("an edited contains keeps a JSON value typed, and a renamed one keeps its value type", () => {
    for (const value of VALUES.filter((v) => typeof v !== "string")) {
      const draft = evaluatorToDraft({ kind: "contains", value } as Evaluator);
      expect(draft.valueIsJson).toBe(true);
      expect(draftToEvaluator({ ...draft, name: "renamed" })).toEqual({ evaluator: { kind: "contains", name: "renamed", value } });
    }
    const text = evaluatorToDraft({ kind: "contains", value: "42" });
    expect(text.valueIsJson).toBe(false);
    expect(draftToEvaluator({ ...text, name: "n" })).toEqual({ evaluator: { kind: "contains", name: "n", value: "42" } });
    const typed = { ...blankEvaluatorDraft("contains"), value: "[1, 2]", valueIsJson: true };
    expect(draftToEvaluator(typed)).toEqual({ evaluator: { kind: "contains", value: [1, 2] } });
    expect(draftToEvaluator({ ...typed, value: "[1," })).toHaveProperty("error");
  });

  it("accepts a USD amount in exponent form when edited", () => {
    const draft = evaluatorToDraft({ kind: "max_cost", usd: "1E-7" });
    expect(draftToEvaluator({ ...draft, name: "cheap" })).toEqual({ evaluator: { kind: "max_cost", name: "cheap", usd: "1E-7" } });
  });

  it("maps a string 422 naming evaluators.N onto row N", () => {
    const error = new ApiError(422, "the test suite is invalid at case.evaluators.0.pattern: pattern is not valid RE2 syntax or is too complex");
    const split = evaluatorApiErrors(error)!;
    expect([...split.rows.entries()]).toEqual([[0, "pattern: pattern is not valid RE2 syntax or is too complex"]]);
    expect(evaluatorApiErrors(new ApiError(422, "the test suite is invalid at evaluators.2: bad"))!.rows.get(2)).toBe("bad");
    expect(evaluatorApiErrors(new ApiError(409, "name already used"))).toBeNull();
  });
});
