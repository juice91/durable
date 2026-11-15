import { afterEach, describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { cleanup, render, screen } from "@testing-library/react";
import { runModels, type TraceSpan } from "@/lib/traces";
import { SpanDetail } from "./SpanDetail";
import { RunModel } from "./RunModel";

function runSpan(attributes: TraceSpan["attributes"]): TraceSpan {
  return {
    id: "run", parent_id: null, kind: "run", name: "agent", start: "2026-11-15T09:00:00Z", end: "2026-11-15T09:00:02Z",
    status: "ok", attributes: { run_id: "R1", run_kind: "agent", input_tokens: 1000, output_tokens: 500, cost_usd: "0.02", ...attributes },
    input: null, output: null, truncated: {},
  };
}

describe("runModels", () => {
  it("is the answering model, with the requested one only where they differ", () => {
    expect(runModels({ model_id: "test:fail-503", answering_model_id: "test:priced" })).toEqual({ model: "test:priced", requested: "test:fail-503" });
    expect(runModels({ model_id: "test:priced", answering_model_id: "test:priced" })).toEqual({ model: "test:priced", requested: null });
    expect(runModels({ model_id: "test:echo", answering_model_id: null })).toEqual({ model: "test:echo", requested: null });
    expect(runModels({ model_id: "test:echo" })).toEqual({ model: "test:echo", requested: null });
    expect(runModels({ model_id: null, answering_model_id: "test:b" })).toEqual({ model: "test:b", requested: null });
    expect(runModels({})).toEqual({ model: null, requested: null });
  });
});

describe("the run's model in Run usage and the Traces lists", () => {
  afterEach(cleanup);

  it("Run usage names the answering model and the requested one of a fallback run", () => {
    render(<SpanDetail span={runSpan({ model_id: "test:fail-503", answering_model_id: "test:priced" })} />);
    expect(screen.getByTestId("span-run-model")).toHaveTextContent("test:priced");
    expect(screen.getByTestId("span-run-requested-model")).toHaveTextContent("test:fail-503");
    // answering_model_id is not repeated in the raw attribute list.
    expect(screen.queryByTestId("span-attributes")?.textContent ?? "").not.toContain("answering_model_id");
  });

  it("Run usage shows one model when the requested model answered, or when none is recorded", () => {
    render(<SpanDetail span={runSpan({ model_id: "test:priced", answering_model_id: "test:priced" })} />);
    expect(screen.getByTestId("span-run-model")).toHaveTextContent("test:priced");
    expect(screen.queryByTestId("span-run-requested-model")).toBeNull();
    cleanup();
    render(<SpanDetail span={runSpan({ model_id: "test:echo" })} />);
    expect(screen.getByTestId("span-run-model")).toHaveTextContent("test:echo");
    expect(screen.queryByTestId("span-run-requested-model")).toBeNull();
  });

  it("the list cell reads the same way as Run usage", () => {
    render(<RunModel run={{ model_id: "test:fail-503", answering_model_id: "test:priced" }} testId="m" />);
    expect(screen.getByTestId("m")).toHaveTextContent(/^test:priced\(requested test:fail-503\)$/);
    expect(screen.getByTestId("m")).toContainElement(screen.getByTitle(/fallback model answered/));
    cleanup();
    render(<RunModel run={{ model_id: null }} testId="m" />);
    expect(screen.getByTestId("m")).toHaveTextContent("-");
  });
});
