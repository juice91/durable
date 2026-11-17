import { describe, expect, it } from "vitest";
import { cronPreview } from "./cronPreview";

describe("cronPreview", () => {
  it("reads common patterns", () => {
    expect(cronPreview("*/5 * * * *")).toBe("Every 5 minutes");
    expect(cronPreview("5 * * * *")).toBe("Every hour at :05");
    expect(cronPreview("* * * * *")).toBe("Every minute");
    expect(cronPreview("0 * * * *")).toBe("Every hour at :00");
    expect(cronPreview("30 9 * * *")).toBe("Every day at 09:30");
  });
  it("returns the raw string when the pattern is unknown", () => {
    expect(cronPreview("0 9 * * 1-5")).toBe("0 9 * * 1-5");
    expect(cronPreview("nonsense")).toBe("nonsense");
  });
});
