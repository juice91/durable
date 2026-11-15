import { describe, expect, it } from "vitest";
import { routes } from "./index";

describe("approval routes", () => {
  it("builds the inbox URL with filters in order, defaults left out", () => {
    expect(routes.approvals()).toBe("/approvals");
    expect(routes.approvals({ status: "pending" })).toBe("/approvals");
    expect(routes.approvals({ status: "decided", kind: "tool", agent: "A" })).toBe("/approvals?status=decided&kind=tool&agent=A");
  });

  it("opens one approval's Sheet", () => {
    expect(routes.approval("R1", "call:1")).toBe("/approvals?approval=R1%3Acall%3A1");
    expect(routes.approval("R1", "s", { kind: "step" })).toBe("/approvals?kind=step&approval=R1%3As");
  });
});
