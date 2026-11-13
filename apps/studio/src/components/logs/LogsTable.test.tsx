import { afterEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { LogsTable } from "./LogsTable";
import { logKey, logRowKeys, type LogItem } from "./logsQuery";

const record: LogItem = {
  ts: "2026-11-13T10:00:00.123456Z",
  level: "info",
  message: "same line",
  service: "eap-worker",
  trace_id: null,
  span_id: null,
  run_id: null,
};

describe("LogsTable row keys", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders identical records in one page without a duplicate-key warning", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<LogsTable items={[record, { ...record }, { ...record, message: "other" }, { ...record }]} caption="Logs" />);
    expect(screen.getAllByTestId("log-row")).toHaveLength(4);
    const keyWarnings = errors.mock.calls.filter((args) => args.some((a) => typeof a === "string" && /same key/i.test(a)));
    expect(keyWarnings).toEqual([]);
  });

  it("keeps the base key for the first copy and suffixes later copies", () => {
    const keys = logRowKeys([record, { ...record }, { ...record, message: "other" }, { ...record }]);
    expect(new Set(keys).size).toBe(4);
    expect(keys[0]).toBe(logKey(record));
    expect(keys[2]).toBe(logKey({ ...record, message: "other" }));
  });
});
