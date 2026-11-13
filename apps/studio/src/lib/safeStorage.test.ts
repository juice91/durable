import { afterEach, describe, expect, test, vi } from "vitest";
import { readStorage, removeStorage, safeLocalStorage, writeStorage } from "./safeStorage";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("safeStorage", () => {
  test("reads, writes and removes through localStorage", () => {
    expect(safeLocalStorage()).toBe(window.localStorage);
    expect(writeStorage("k", "v")).toBe(true);
    expect(readStorage("k")).toBe("v");
    expect(removeStorage("k")).toBe(true);
    expect(readStorage("k")).toBeNull();
  });

  test("throwing storage (blocked, quota) falls back instead of throwing", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readStorage("k")).toBeNull();
    expect(writeStorage("k", "v")).toBe(false);
    expect(removeStorage("k")).toBe(false);
  });

  test("a throwing localStorage accessor means no storage", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(safeLocalStorage()).toBeNull();
    expect(readStorage("k")).toBeNull();
    expect(writeStorage("k", "v")).toBe(false);
  });
});
