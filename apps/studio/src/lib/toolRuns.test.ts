import { describe, expect, it } from "vitest";
import { describeToolRun, mapToolRunError, NEEDS_ATTENTION_MESSAGE, prettyJson } from "./toolRuns";
import type { RunEvent } from "./runs";

function failedEvent(data: Record<string, unknown>): RunEvent {
  return { seq: 3, type: "run_failed", data, ts: "2026-11-13T00:00:00Z" };
}

describe("mapToolRunError", () => {
  it("maps a 422 validation list onto input fields by loc", () => {
    const error = mapToolRunError(422, {
      detail: [
        { loc: ["body", "input", "subject"], msg: "Field required", type: "missing" },
        { loc: ["body", "input", "options", "retries"], msg: "Input should be a valid integer", type: "int_parsing" },
        { loc: ["body", "input", "options", "retries"], msg: "second problem", type: "x" },
      ],
    });
    expect(error.status).toBe(422);
    expect(error.fieldErrors).toEqual({
      subject: "Field required",
      "options.retries": "Input should be a valid integer; second problem",
    });
    expect(error.banner).toBe("Fix the highlighted input fields");
  });

  it("puts errors that are not about an input field in the banner", () => {
    const error = mapToolRunError(422, {
      detail: [
        { loc: ["body", "input"], msg: "Input should be a valid dictionary", type: "dict_type" },
        { loc: ["body", "extra"], msg: "Extra inputs are not permitted", type: "extra_forbidden" },
      ],
    });
    expect(error.fieldErrors).toEqual({});
    expect(error.banner).toBe("body.input: Input should be a valid dictionary; body.extra: Extra inputs are not permitted");
  });

  it("shows a string detail (tool not allowed) as a banner", () => {
    const detail = "tool 'tickets.create' is not allowed on this platform (not in the EAP_ALLOWED_TOOLS allowlist)";
    const error = mapToolRunError(422, { detail });
    expect(error.banner).toBe(detail);
    expect(error.fieldErrors).toEqual({});
  });

  it("falls back to a status message without a JSON detail", () => {
    expect(mapToolRunError(404, null).banner).toBe("Tool not found");
    expect(mapToolRunError(503, {}).banner).toBe("The run could not be started; try again");
    expect(mapToolRunError(500, null).banner).toBe("HTTP 500");
  });
});

describe("describeToolRun", () => {
  it("shows needs_attention with the explicit check-before-retrying message", () => {
    const view = describeToolRun("needs_attention", { output: null, error: "side effect outcome unknown" }, null);
    expect(view.tone).toBe("attention");
    expect(view.title).toBe(NEEDS_ATTENTION_MESSAGE);
    expect(view.title).toBe("Needs attention: the tool may have run; check before retrying");
    expect(view.error).toBe("side effect outcome unknown");
  });

  it("takes needs_attention from the run_failed event even if the status frame lags", () => {
    const view = describeToolRun("running", null, failedEvent({ status: "needs_attention", error: "unknown", error_code: "side_effect_unknown" }));
    expect(view.tone).toBe("attention");
    expect(view.status).toBe("needs_attention");
    expect(view.errorCode).toBe("side_effect_unknown");
  });

  it("shows a failure with its error and error_code", () => {
    const view = describeToolRun("failed", { output: null, error: "boom" }, failedEvent({ status: "failed", error: "boom", error_code: "tool_error" }));
    expect(view).toMatchObject({ tone: "error", title: "Failed", error: "boom", errorCode: "tool_error" });
  });

  it("pretty-prints a completed result", () => {
    const view = describeToolRun("completed", { output: '{"now":"2026-11-13","n":1}', error: null }, null);
    expect(view.tone).toBe("success");
    expect(view.result).toBe('{\n  "now": "2026-11-13",\n  "n": 1\n}');
  });

  it("is pending while queued or running", () => {
    expect(describeToolRun("queued", null, null)).toMatchObject({ tone: "pending", title: "Queued" });
    expect(describeToolRun("running", null, null)).toMatchObject({ tone: "pending", title: "Running" });
  });
});

describe("prettyJson", () => {
  it("returns non-JSON text unchanged", () => {
    expect(prettyJson("plain")).toBe("plain");
    expect(prettyJson(null)).toBe("");
  });
});
