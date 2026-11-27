import { describe, expect, it } from "vitest";
import { addable, grantNotes, permissionDeniedMessage, type AccessStatus } from "./agentGrants";

const base: AccessStatus = {
  subject: { kind: "agent", id: "a" }, supported: true, enforced: true, can_manage: true,
  options: { mcp_servers: [{ id: "1", name: "tickets" }, { id: "2", name: "wiki" }], secrets: ["a", "b"] },
  grants: [{ kind: "mcp_server", id: "1", name: "tickets", credentials: [], used_by_latest: null }, { kind: "secret", id: "a", name: "a", credentials: [], used_by_latest: true }],
};

describe("agent grants helpers", () => {
  it("offers only what is not granted, and nothing without options", () => {
    expect(addable(base).map((o) => o.label)).toEqual(["MCP server · wiki", "Secret · b"]);
    expect(addable({ ...base, options: null })).toEqual([]);
  });

  it("notes the credentials, who granted it and an unused grant", () => {
    expect(grantNotes({ kind: "mcp_server", id: "1", name: "t", credentials: ["A", "B"], used_by_latest: false, granted_by: "ann" })).toEqual([
      "This server uses credentials: A, B",
      "Granted by ann",
      "Granted, unused by the latest version",
    ]);
    expect(grantNotes({ kind: "secret", id: "a", name: "a", credentials: [], used_by_latest: null })).toEqual([]);
  });

  it("reads the refusal message of a permission_denied event", () => {
    expect(permissionDeniedMessage({ message: "agent X is not authorized to use secret y" })).toBe("agent X is not authorized to use secret y");
    expect(permissionDeniedMessage({})).toBeNull();
    expect(permissionDeniedMessage(null)).toBeNull();
  });
});
