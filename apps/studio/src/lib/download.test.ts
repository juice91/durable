import { afterEach, describe, expect, it, vi } from "vitest";
import { downloadJson } from "./download";

afterEach(() => vi.restoreAllMocks());

describe("downloadJson", () => {
  it("revokes the object URL after the click, not during it", () => {
    vi.useFakeTimers();
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: revoke });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    downloadJson("a.json", { a: 1 });
    expect(revoke).not.toHaveBeenCalled();
    vi.runAllTimers();
    expect(revoke).toHaveBeenCalledWith("blob:x");
    vi.useRealTimers();
  });
});
