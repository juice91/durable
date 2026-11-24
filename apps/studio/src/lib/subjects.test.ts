import { beforeEach, describe, expect, it } from "vitest";
import { actorLabel, type AuditRow } from "./audit";
import { rememberMember, rememberSubject, resetSubjectsForTests, subjectLabel } from "./subjects";

const row = (over: Partial<AuditRow>): AuditRow => ({
  id: "1", at: "2026-01-01T00:00:00Z", actor_kind: "user", actor_subject: "x", on_behalf_of: null, action: "a",
  resource_kind: null, resource_id: null, outcome: "success", request_id: null, run_id: null, detail: null, ...over,
});

beforeEach(resetSubjectsForTests);

describe("subject labels", () => {
  it("a legacy e-mail subject (cfaccess rows) is shown as it is", () => {
    expect(subjectLabel("old@example.com")).toBe("old@example.com");
    expect(actorLabel(row({ actor_subject: "old@example.com" }))).toBe("old@example.com");
  });

  it("a zitadel:<sub> audit actor shows the e-mail the row carries", () => {
    expect(actorLabel(row({ actor_subject: "zitadel:u-1", detail: { actor_email: "Zed@Example.com" } }))).toBe("zed@example.com");
    expect(subjectLabel("zitadel:u-1")).toBe("zed@example.com"); // and the directory now knows it for approvals and versions
  });

  it("the Members list introduces ids, so decided_by shows an e-mail", () => {
    rememberMember("u-2", "bob@example.test");
    expect(subjectLabel("zitadel:u-2")).toBe("bob@example.test");
  });

  it("an unknown id is a short labelled id, never the raw subject", () => {
    expect(subjectLabel("zitadel:abcdef1234567890")).toBe("ZITADEL user abcdef12…");
  });

  it("on_behalf_of is labelled too, and an unchanged subject is not remembered as itself", () => {
    rememberSubject("a@b.co", "a@b.co");
    rememberSubject("zitadel:u-3", "c@d.co");
    expect(actorLabel(row({ actor_subject: "zitadel:run", on_behalf_of: "zitadel:u-3" }))).toContain("(for c@d.co)");
  });
});
