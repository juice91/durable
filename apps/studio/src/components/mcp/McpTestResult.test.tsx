import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { describe, expect, it, vi } from "vitest";
import { McpTestResult } from "./McpTestResult";

const tool = { name: "read", description: null, input_schema: {} };

describe("McpTestResult wording", () => {
  it("says the server was started (idle for N min) and tested OK", () => {
    render(<McpTestResult result={{ status: "ok", tools: [tool], runtime: { state: "stopped", started: true, waited_ms: 4200, idle_minutes: 30 } }} />);
    expect(screen.getByTestId("mcp-test-started")).toHaveTextContent("Started the server (idle for 30 min) and tested it: OK");
    expect(screen.getByTestId("mcp-test-tools")).toHaveTextContent("Connected. 1 tool discovered.");
  });

  it("says nothing about starting when the server was already running", () => {
    render(<McpTestResult result={{ status: "ok", tools: [tool], runtime: { state: "running", started: false, waited_ms: 0 } }} />);
    expect(screen.queryByTestId("mcp-test-started")).not.toBeInTheDocument();
  });

  it("offers Retry while the server is still starting, not 'Connection failed'", () => {
    const onRetry = vi.fn();
    render(<McpTestResult result={{ status: "error", error_code: "runtime_starting", tools: [], runtime: { state: "stopped", started: false, waited_ms: 60000 } }} onRetry={onRetry} />);
    expect(screen.getByTestId("mcp-test-starting")).toHaveTextContent("Server is starting, test again in a moment");
    expect(screen.queryByText("Connection failed")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("mcp-test-retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("keeps 'Connection failed' for every other code, runtime_not_running included", () => {
    render(<McpTestResult result={{ status: "error", error_code: "runtime_not_running", tools: [], runtime: { state: "failed", started: false, waited_ms: 0, last_error: "start_failed: X" } }} />);
    expect(screen.getByTestId("mcp-test-error")).toHaveTextContent("Connection failed");
    expect(screen.getByTestId("mcp-test-error")).toHaveTextContent("runtime_not_running");
  });
});
