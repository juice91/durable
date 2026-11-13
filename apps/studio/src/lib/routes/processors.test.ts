import { describe, expect, it } from "vitest";
import { RouteIdError, routes } from "./index";

describe("processor routes", () => {
  it("builds the list and per-type URLs", () => {
    expect(routes.processors()).toBe("/processors");
    expect(routes.processor("pii_redact")).toBe("/processors/pii_redact");
  });

  it("encodes the type segment and refuses dot segments", () => {
    expect(routes.processor("a/b c")).toBe("/processors/a%2Fb%20c");
    expect(() => routes.processor("..")).toThrow(RouteIdError);
  });
});
