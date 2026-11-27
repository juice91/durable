import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { EventTimeline } from "./EventTimeline";
import type { RunEvent } from "@/lib/runs";

describe("EventTimeline permission_denied", () => {
  it("reads \"agent X is not authorized to use Y\" with the reason", () => {
    const event: RunEvent = {
      seq: 1, type: "permission_denied", ts: "2026-11-25T09:00:00Z",
      data: { code: "permission_denied", reason: "not_granted", resource_kind: "mcp_server", name: "tickets", message: "agent Desk is not authorized to use MCP server tickets" } as RunEvent["data"],
    };
    render(<EventTimeline events={[event]} currentPartialText={{}} />);
    const line = screen.getByTestId("event-permission-denied");
    expect(line).toHaveTextContent("Blocked");
    expect(line).toHaveTextContent("agent Desk is not authorized to use MCP server tickets");
    expect(line).toHaveTextContent("Reason: not granted");
  });
});
