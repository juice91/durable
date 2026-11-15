/**
 * AC.16 acceptance: creating and editing suites and cases: all 8 evaluators, the judge model picker,
 * expected output as text and JSON, and the API's 422s, against a fake of the AC.12 API.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { routes } from "@/lib/routes";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { TestsTab } from "./TestsTab";
import { AGENT, SUITE, callsTo, installFakeApi, suiteOf, type FakeApi } from "./evalsFakeApi";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

let fake: FakeApi;
beforeEach(() => {
  fake = installFakeApi();
});
afterEach(() => {
  cleanup();
  fake.restore();
});

const suiteHref = (extra = "") => `${routes.agentTests(AGENT, { suite: SUITE })}${extra}`;
function renderAt(href: string) {
  nextRouter.reset([href]);
  return render(
    <TooltipProvider>
      <TestsTab agentId={AGENT} />
    </TooltipProvider>,
  );
}
const set = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });
const lastRow = (sheet: HTMLElement) => within(sheet).getAllByTestId("evaluator-row").at(-1)!;
async function addEvaluator(sheet: HTMLElement, kind: string) {
  fireEvent.click(within(sheet).getByTestId("evaluator-add"));
  set(within(lastRow(sheet)).getByTestId("evaluator-kind"), kind);
  return lastRow(sheet);
}
async function openSuiteSheet(evaluators: unknown[] = []) {
  fake.state.suites = [suiteOf({ evaluators: evaluators as never })];
  renderAt(suiteHref("&edit=suite"));
  return screen.findByTestId("eval-suite-sheet");
}
const patchBody = (path: RegExp) => callsTo(fake.state, "PATCH", path).at(-1)?.body as Record<string, unknown>;

describe("a new suite", () => {
  it("is created with an Equals expected evaluator and opens", async () => {
    renderAt(routes.agentTests(AGENT));
    fireEvent.click((await screen.findAllByTestId("eval-suite-new"))[0]);
    const dialog = await screen.findByTestId("eval-suite-create-dialog");
    expect(within(dialog).getByTestId("eval-suite-create-submit")).toBeDisabled();
    set(within(dialog).getByTestId("eval-suite-create-name"), " Billing ");
    fireEvent.click(within(dialog).getByTestId("eval-suite-create-submit"));
    await waitFor(() => expect(nextRouter.url).toBe(routes.agentTests(AGENT, { suite: "s2" })));
    expect(callsTo(fake.state, "POST", /eval-suites$/)[0].body).toEqual({ name: "Billing", description: null, evaluators: [{ kind: "equals_expected" }] });
  });

  it("shows the API's 422 for the name and stays open", async () => {
    fake.fail({ method: "POST", path: /eval-suites$/, status: 422, body: { detail: [{ loc: ["body", "name"], msg: "name must not be blank", type: "value_error" }] } });
    renderAt(routes.agentTests(AGENT));
    fireEvent.click((await screen.findAllByTestId("eval-suite-new"))[0]);
    const dialog = await screen.findByTestId("eval-suite-create-dialog");
    set(within(dialog).getByTestId("eval-suite-create-name"), "x");
    fireEvent.click(within(dialog).getByTestId("eval-suite-create-submit"));
    expect(await within(dialog).findByTestId("eval-suite-create-error")).toHaveTextContent("name must not be blank");
    expect(nextRouter.url).toBe(routes.agentTests(AGENT));
  });
});

describe("suite evaluators: all 8 kinds", () => {
  it("saves every kind in its API shape", async () => {
    const sheet = await openSuiteSheet();
    const eq = await addEvaluator(sheet, "equals_expected");
    set(within(eq).getByTestId("evaluator-name"), "exact");
    const contains = await addEvaluator(sheet, "contains");
    set(within(contains).getByTestId("evaluator-value"), "ok");
    fireEvent.click(within(contains).getByTestId("evaluator-case-sensitive"));
    set(within(await addEvaluator(sheet, "regex")).getByTestId("evaluator-pattern"), "^ok");
    set(within(await addEvaluator(sheet, "schema_valid")).getByTestId("evaluator-schema"), '{"type":"object"}');
    const field = await addEvaluator(sheet, "field_equals");
    set(within(field).getByTestId("evaluator-path"), "a.b");
    set(within(field).getByTestId("evaluator-field-value"), '"x"');
    set(within(await addEvaluator(sheet, "max_duration")).getByTestId("evaluator-seconds"), "2.5");
    set(within(await addEvaluator(sheet, "max_cost")).getByTestId("evaluator-usd"), "0.05");
    const judge = await addEvaluator(sheet, "llm_judge");
    set(within(judge).getByTestId("evaluator-rubric"), "Polite");
    fireEvent.click(within(judge).getByTestId("evaluator-include-input"));
    set(within(judge).getByTestId("evaluator-threshold"), "0.7");
    expect(within(sheet).getAllByTestId("evaluator-row").map((r) => r.getAttribute("data-kind"))).toEqual([
      "equals_expected", "contains", "regex", "schema_valid", "field_equals", "max_duration", "max_cost", "llm_judge",
    ]);
    fireEvent.click(within(sheet).getByTestId("eval-suite-save"));
    await waitFor(() => expect(callsTo(fake.state, "PATCH", /eval-suites\/s1$/)).toHaveLength(1));
    expect(patchBody(/eval-suites\/s1$/).evaluators).toEqual([
      { kind: "equals_expected", name: "exact" },
      { kind: "contains", value: "ok", case_sensitive: false },
      { kind: "regex", pattern: "^ok" },
      { kind: "schema_valid", json_schema: { type: "object" } },
      { kind: "field_equals", path: "a.b", value: "x" },
      { kind: "max_duration", seconds: 2.5 },
      { kind: "max_cost", usd: "0.05" },
      { kind: "llm_judge", rubric: "Polite", model: { provider: "test", name: "judge" }, include_input: true, include_expected: false, pass_threshold: 0.7 },
    ]);
  });

  it("reopens every stored evaluator with its settings", async () => {
    const stored = [
      { kind: "contains", value: "ok", case_sensitive: false },
      { kind: "regex", pattern: "a+" },
      { kind: "field_equals", path: "x.y", value: 3 },
      { kind: "max_cost", usd: "0.5" },
      { kind: "llm_judge", name: "tone", rubric: "Kind", model: { provider: "openai", name: "gpt-4.1-mini" }, include_input: false, include_expected: true, pass_threshold: 0.9 },
    ];
    const sheet = await openSuiteSheet(stored);
    const rows = within(sheet).getAllByTestId("evaluator-row");
    expect(within(rows[0]).getByTestId("evaluator-case-sensitive")).not.toBeChecked();
    expect(within(rows[1]).getByTestId("evaluator-pattern")).toHaveValue("a+");
    expect(within(rows[2]).getByTestId("evaluator-field-value")).toHaveValue("3");
    expect(within(rows[3]).getByTestId("evaluator-usd")).toHaveValue("0.5");
    expect(within(rows[4]).getByTestId("evaluator-name")).toHaveValue("tone");
    expect(within(rows[4]).getByTestId("evaluator-include-expected")).toBeChecked();
    expect(within(rows[4]).getByTestId("evaluator-threshold")).toHaveValue("0.9");
  });

  it("removes an evaluator", async () => {
    const sheet = await openSuiteSheet([{ kind: "equals_expected" }, { kind: "regex", pattern: "a" }]);
    fireEvent.click(within(within(sheet).getAllByTestId("evaluator-row")[0]).getByTestId("evaluator-remove"));
    fireEvent.click(within(sheet).getByTestId("eval-suite-save"));
    await waitFor(() => expect(patchBody(/eval-suites\/s1$/)).toBeDefined());
    expect(patchBody(/eval-suites\/s1$/).evaluators).toEqual([{ kind: "regex", pattern: "a" }]);
  });
});

describe("evaluator validation", () => {
  const invalid: [string, Record<string, string>, RegExp][] = [
    ["regex", {}, /Enter a pattern/],
    ["schema_valid", { "evaluator-schema": "{nope" }, /not valid JSON/],
    ["field_equals", { "evaluator-path": "a", "evaluator-field-value": "oops" }, /must be JSON/],
    ["max_duration", { "evaluator-seconds": "0" }, /Seconds must be more than 0/],
    ["max_cost", { "evaluator-usd": "abc" }, /USD must be more than 0/],
    ["llm_judge", {}, /Enter a rubric/],
  ];
  it.each(invalid)("a %s evaluator with a bad value shows its error on its own row and sends nothing", async (kind, fields, message) => {
    const sheet = await openSuiteSheet([{ kind: "equals_expected" }]);
    const row = await addEvaluator(sheet, kind);
    for (const [id, value] of Object.entries(fields)) set(within(row).getByTestId(id), value);
    fireEvent.click(within(sheet).getByTestId("eval-suite-save"));
    expect(await within(row).findByTestId("evaluator-error")).toHaveTextContent(message);
    expect(within(within(sheet).getAllByTestId("evaluator-row")[0]).queryByTestId("evaluator-error")).not.toBeInTheDocument();
    expect(callsTo(fake.state, "PATCH", /eval-suites/)).toHaveLength(0);
  });

  it("refuses duplicate names, naming the second evaluator", async () => {
    const sheet = await openSuiteSheet();
    set(within(await addEvaluator(sheet, "regex")).getByTestId("evaluator-pattern"), "a");
    set(within(lastRow(sheet)).getByTestId("evaluator-name"), "same");
    const second = await addEvaluator(sheet, "regex");
    set(within(second).getByTestId("evaluator-pattern"), "b");
    set(within(second).getByTestId("evaluator-name"), "same");
    fireEvent.click(within(sheet).getByTestId("eval-suite-save"));
    expect(await within(second).findByTestId("evaluator-error")).toHaveTextContent("used twice");
    expect(callsTo(fake.state, "PATCH", /eval-suites/)).toHaveLength(0);
  });

  it("stops adding at 10 suite evaluators", async () => {
    const sheet = await openSuiteSheet(Array.from({ length: 10 }, () => ({ kind: "equals_expected" })));
    expect(within(sheet).getByTestId("evaluator-add")).toBeDisabled();
    expect(within(sheet).getByText("At most 10 evaluators.")).toBeInTheDocument();
  });
});

describe("the judge model picker", () => {
  it("offers the allowlisted concrete models only, never a wildcard", async () => {
    const sheet = await openSuiteSheet();
    const judge = await addEvaluator(sheet, "llm_judge");
    const select = await within(judge).findByTestId("evaluator-model");
    expect(within(select).getAllByRole("option").map((o) => o.getAttribute("value"))).toEqual(["test:judge", "openai:gpt-4.1-mini"]);
    expect(select).toHaveValue("test:judge");
  });

  it("reopens a stored allowlisted judge model in the picker, not as typed text", async () => {
    const sheet = await openSuiteSheet([{ kind: "llm_judge", rubric: "r", model: { provider: "test", name: "judge" } }]);
    const row = within(sheet).getByTestId("evaluator-row");
    expect(await within(row).findByTestId("evaluator-model")).toHaveValue("test:judge");
  });

  it("lets a provider:name be typed, and keeps a stored model that is no longer allowed", async () => {
    const sheet = await openSuiteSheet([{ kind: "llm_judge", rubric: "r", model: { provider: "anthropic", name: "claude-x" } }]);
    const row = within(sheet).getByTestId("evaluator-row");
    expect(await within(row).findByTestId("evaluator-model-text")).toHaveValue("anthropic:claude-x");
    fireEvent.click(within(row).getByTestId("evaluator-model-toggle"));
    expect(within(row).getByTestId("evaluator-model")).toHaveValue("");
    fireEvent.click(within(row).getByTestId("evaluator-model-toggle"));
    set(within(row).getByTestId("evaluator-model-text"), "openai:gpt-4.1");
    fireEvent.click(within(sheet).getByTestId("eval-suite-save"));
    await waitFor(() => expect(callsTo(fake.state, "PATCH", /eval-suites\/s1$/)).toHaveLength(1));
    expect((patchBody(/eval-suites\/s1$/).evaluators as { model: unknown }[])[0].model).toEqual({ provider: "openai", name: "gpt-4.1" });
  });

  it("refuses a judge without a usable model", async () => {
    const sheet = await openSuiteSheet([{ kind: "llm_judge", rubric: "r", model: { provider: "anthropic", name: "claude-x" } }]);
    const row = within(sheet).getByTestId("evaluator-row");
    set(await within(row).findByTestId("evaluator-model-text"), "nocolon");
    fireEvent.click(within(sheet).getByTestId("eval-suite-save"));
    expect(await within(row).findByTestId("evaluator-error")).toHaveTextContent("Choose a judge model");
  });
});

describe("case editing", () => {
  const openCase = async (id = "new") => {
    renderAt(suiteHref(`&edit=${id}`));
    return screen.findByTestId("eval-case-sheet");
  };
  const save = (sheet: HTMLElement) => fireEvent.click(within(sheet).getByTestId("eval-case-save"));
  const caseCall = (method: string) => callsTo(fake.state, method, /eval-suites\/s1\/cases/).at(-1)?.body as Record<string, unknown>;

  it("adds a case with a text expected output, metadata and its own evaluator", async () => {
    const sheet = await openCase();
    set(within(sheet).getByTestId("eval-case-name"), "third");
    set(within(sheet).getByTestId("eval-case-input"), "Where is my order?");
    fireEvent.click(within(sheet).getByRole("radio", { name: "Text" }));
    set(within(sheet).getByTestId("eval-case-expected"), "In transit");
    set(within(sheet).getByTestId("eval-case-metadata"), '{"topic": "orders", "n": 2}');
    set(within(await addEvaluator(sheet, "contains")).getByTestId("evaluator-value"), "transit");
    save(sheet);
    await waitFor(() => expect(callsTo(fake.state, "POST", /cases$/)).toHaveLength(1));
    expect(caseCall("POST")).toEqual({
      name: "third", input: "Where is my order?", expected_output: "In transit", metadata: { topic: "orders", n: 2 }, evaluators: [{ kind: "contains", value: "transit" }],
    });
    await waitFor(() => expect(screen.queryByTestId("eval-case-sheet")).not.toBeInTheDocument());
    expect(await screen.findAllByTestId("eval-case-row")).toHaveLength(3);
  });

  it("sends a JSON expected output parsed, a text one as a string, and none as null", async () => {
    const sheet = await openCase("c1");
    expect(within(sheet).getByTestId("eval-case-expected")).toHaveAttribute("data-mode", "text");
    fireEvent.click(within(sheet).getByRole("radio", { name: "JSON" }));
    set(within(sheet).getByTestId("eval-case-expected"), '{"ok": [1, 2]}');
    save(sheet);
    await waitFor(() => expect(callsTo(fake.state, "PATCH", /cases\/c1$/)).toHaveLength(1));
    expect(caseCall("PATCH").expected_output).toEqual({ ok: [1, 2] });
  });

  it("opens a stored object expected output as JSON, and a stored null as None", async () => {
    fake.state.suites = [suiteOf({ cases: [{ ...suiteOf().cases[0], expected_output: { a: 1 } }, { ...suiteOf().cases[1], expected_output: null }] })];
    const sheet = await openCase("c1");
    expect(within(sheet).getByTestId("eval-case-expected")).toHaveAttribute("data-mode", "json");
    expect(within(sheet).getByRole("radio", { name: "JSON" })).toBeChecked();
    cleanup();
    const none = await openCase("c2");
    expect(within(none).getByRole("radio", { name: "None" })).toBeChecked();
    expect(within(none).queryByTestId("eval-case-expected")).not.toBeInTheDocument();
    save(none);
    await waitFor(() => expect(callsTo(fake.state, "PATCH", /cases\/c2$/)).toHaveLength(1));
    expect(caseCall("PATCH").expected_output).toBeNull();
  });

  it("refuses invalid JSON, bad metadata and a blank input before sending", async () => {
    const sheet = await openCase();
    set(within(sheet).getByTestId("eval-case-name"), "x");
    save(sheet);
    expect(await within(sheet).findByTestId("eval-case-error")).toHaveTextContent("Enter the input");
    set(within(sheet).getByTestId("eval-case-input"), "hi");
    fireEvent.click(within(sheet).getByRole("radio", { name: "JSON" }));
    set(within(sheet).getByTestId("eval-case-expected"), "{bad");
    save(sheet);
    expect(await within(sheet).findByTestId("eval-case-error")).toHaveTextContent("Expected output is not valid JSON");
    fireEvent.click(within(sheet).getByRole("radio", { name: "None" }));
    set(within(sheet).getByTestId("eval-case-metadata"), '{"a": {"b": 1}}');
    save(sheet);
    expect(await within(sheet).findByTestId("eval-case-error")).toHaveTextContent("Metadata values must be");
    expect(callsTo(fake.state, "POST", /cases$/)).toHaveLength(0);
  });

  it("deletes a case after a confirmation", async () => {
    const sheet = await openCase("c1");
    fireEvent.click(within(sheet).getByTestId("eval-case-delete"));
    fireEvent.click(await screen.findByRole("button", { name: "Delete case" }));
    await waitFor(() => expect(callsTo(fake.state, "DELETE", /cases\/c1$/)).toHaveLength(1));
    await waitFor(() => expect(screen.getAllByTestId("eval-case-row")).toHaveLength(1));
  });
});

describe("the API's 422 on a case or suite", () => {
  const detail = (loc: (string | number)[], msg: string) => ({ detail: [{ loc: ["body", ...loc], msg, type: "value_error" }] });
  const fillCase = async () => {
    renderAt(suiteHref("&edit=new"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    set(within(sheet).getByTestId("eval-case-name"), "dup");
    set(within(sheet).getByTestId("eval-case-input"), "hi");
    return sheet;
  };

  it("shows the message, keeps the Sheet open and the typed values", async () => {
    fake.fail({ method: "POST", path: /cases$/, status: 422, body: detail(["name"], "a case named 'dup' already exists") });
    const sheet = await fillCase();
    fireEvent.click(within(sheet).getByTestId("eval-case-save"));
    expect(await within(sheet).findByTestId("eval-case-error")).toHaveTextContent("a case named 'dup' already exists");
    expect(within(sheet).getByTestId("eval-case-name")).toHaveValue("dup");
    expect(within(sheet).getByTestId("eval-case-save")).toBeEnabled();
    expect(nextRouter.url).toContain("edit=new");
  });

  it("maps an evaluator's 422 (loc body.evaluators.N) onto that evaluator's row", async () => {
    fake.fail({ method: "POST", path: /cases$/, status: 422, body: detail(["evaluators", 1, "regex", "pattern"], "pattern uses an unsupported RE2 feature") });
    const sheet = await fillCase();
    set(within(await addEvaluator(sheet, "equals_expected")).getByTestId("evaluator-name"), "a");
    set(within(await addEvaluator(sheet, "regex")).getByTestId("evaluator-pattern"), "(?=x)");
    fireEvent.click(within(sheet).getByTestId("eval-case-save"));
    const rows = await waitFor(() => {
      const found = within(sheet).getAllByTestId("evaluator-row");
      expect(within(found[1]).getByTestId("evaluator-error")).toHaveTextContent("unsupported RE2 feature");
      return found;
    });
    expect(within(rows[0]).queryByTestId("evaluator-error")).not.toBeInTheDocument();
  });

  it("shows a suite 422 in the suite Sheet", async () => {
    fake.fail({ method: "PATCH", path: /eval-suites\/s1$/, status: 422, body: detail(["evaluators"], "at most 10 evaluators") });
    renderAt(suiteHref("&edit=suite"));
    const sheet = await screen.findByTestId("eval-suite-sheet");
    set(within(sheet).getByTestId("eval-suite-name"), "Renamed");
    fireEvent.click(within(sheet).getByTestId("eval-suite-save"));
    expect(await within(sheet).findByTestId("eval-suite-error")).toHaveTextContent("at most 10 evaluators");
  });

  it("a successful retry after the 422 closes the Sheet", async () => {
    fake.fail({ method: "POST", path: /cases$/, status: 422, body: detail(["name"], "bad"), once: true });
    const sheet = await fillCase();
    fireEvent.click(within(sheet).getByTestId("eval-case-save"));
    await within(sheet).findByTestId("eval-case-error");
    fireEvent.click(within(sheet).getByTestId("eval-case-save"));
    await waitFor(() => expect(screen.queryByTestId("eval-case-sheet")).not.toBeInTheDocument());
  });
});

describe("leaving with unsaved edits", () => {
  it("asks before Cancel discards edits, and not when nothing changed", async () => {
    renderAt(suiteHref("&edit=c1"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    set(within(sheet).getByTestId("eval-case-name"), "renamed");
    fireEvent.click(within(sheet).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Discard changes?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(screen.queryByTestId("eval-case-sheet")).not.toBeInTheDocument());
    expect(callsTo(fake.state, "PATCH", /cases/)).toHaveLength(0);
  });
});
