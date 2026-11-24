import { describe, expect, it } from "vitest";
import { actorLabel, auditParams, EMPTY_AUDIT_FILTERS, type AuditRow } from "./audit";

describe("auditParams", () => {
  it("leaves empty filters out and always sends a limit", () => {
    expect(auditParams(EMPTY_AUDIT_FILTERS, null).toString()).toBe("limit=50");
  });
  it("sends the filters, trimmed, times as instants, and the cursor", () => {
    const p = auditParams({ ...EMPTY_AUDIT_FILTERS, actor: " a@b.co ", outcome: "denied", since: "2026-11-01T00:00" }, "cur");
    expect(p.get("actor")).toBe("a@b.co");
    expect(p.get("outcome")).toBe("denied");
    expect(p.get("since")).toMatch(/^2026-1[01]-\d\dT.*Z$/);
    expect(p.get("cursor")).toBe("cur");
  });
});

describe("actorLabel", () => {
  const row = { actor_subject: "key1", on_behalf_of: "a@b.co" } as AuditRow;
  it("names who the principal acted for", () => {
    expect(actorLabel(row)).toBe("key1 (for a@b.co)");
    expect(actorLabel({ ...row, on_behalf_of: null })).toBe("key1");
  });
});
