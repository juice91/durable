// LP.12 review: the run view's iteration pagers as URL state (`?iter=`, `?failedOnly=`).
import { describe, expect, it } from "vitest";
import { failedOnlyParam, iterationParam, parseFailedOnlyParam, parseIterationParam } from "./workflows";

describe("?iter= and ?failedOnly=", () => {
  it("parses iteration scopes into choices keyed by loop instance, dropping malformed entries", () => {
    expect(parseIterationParam("each.41,outer.1/inner.2, BAD,each,x.-1,outer.01")).toEqual({ each: 41, "outer.1/inner": 2 });
    expect(parseIterationParam(null)).toEqual({});
    expect(parseIterationParam("")).toEqual({});
  });

  it("writes a sorted value (one choice, one URL), null when empty, and round-trips", () => {
    const selection = { "outer.1/inner": 2, each: 41 };
    expect(iterationParam(selection)).toBe("each.41,outer.1/inner.2");
    expect(parseIterationParam(iterationParam(selection))).toEqual(selection);
    expect(iterationParam({})).toBeNull();
    expect(iterationParam({ BAD: 1, each: -1, ok: 1.5 })).toBeNull();
  });

  it("parses and writes failed-only loop instances", () => {
    expect([...parseFailedOnlyParam("outer.1/inner,each,Bad,each.2")]).toEqual(["outer.1/inner", "each"]);
    expect(failedOnlyParam(new Set(["outer.1/inner", "each"]))).toBe("each,outer.1/inner");
    expect(failedOnlyParam(new Set())).toBeNull();
  });
});
