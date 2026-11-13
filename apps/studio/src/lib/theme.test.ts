import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyTheme,
  getStoredTheme,
  resolveTheme,
  setTheme,
  storeTheme,
  THEME_STORAGE_KEY,
  themeScript,
  type ThemePreference,
} from "./theme";

function mockMatchMedia(dark: boolean) {
  const matchMedia = vi.fn((query: string) => ({
    matches: query === "(prefers-color-scheme: dark)" ? dark : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: matchMedia });
}

const throwingStorage = {
  getItem: () => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("blocked");
  },
} as unknown as Storage;

function resetRoot() {
  document.documentElement.className = "";
  document.documentElement.removeAttribute("style");
}

beforeEach(() => {
  window.localStorage.clear();
  resetRoot();
});

afterEach(() => {
  // @ts-expect-error -- jsdom has no matchMedia; tests add it per case
  delete window.matchMedia;
  vi.restoreAllMocks();
});

describe("resolveTheme", () => {
  it.each([
    ["system", false, "light"],
    ["system", true, "dark"],
    ["light", false, "light"],
    ["light", true, "light"],
    ["dark", false, "dark"],
    ["dark", true, "dark"],
  ] as const)("%s with dark media %s resolves to %s", (pref, mediaDark, expected) => {
    expect(resolveTheme(pref as ThemePreference, mediaDark)).toBe(expected);
  });
});

describe("theme storage", () => {
  it("uses the studio.theme key and defaults to system", () => {
    expect(THEME_STORAGE_KEY).toBe("studio.theme");
    expect(getStoredTheme()).toBe("system");
    window.localStorage.setItem("studio.theme", "dark");
    expect(getStoredTheme()).toBe("dark");
  });

  it("ignores an unknown stored value", () => {
    window.localStorage.setItem("studio.theme", "sepia");
    expect(getStoredTheme()).toBe("system");
  });

  it("falls back to system when storage throws, and storing reports failure", () => {
    expect(getStoredTheme(throwingStorage)).toBe("system");
    expect(storeTheme("dark", throwingStorage)).toBe(false);
    expect(() => setTheme("dark", throwingStorage)).not.toThrow();
  });

  it("setTheme stores the preference and applies it", () => {
    mockMatchMedia(false);
    expect(setTheme("dark")).toBe("dark");
    expect(window.localStorage.getItem("studio.theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe("dark");

    expect(setTheme("system")).toBe("light");
    expect(window.localStorage.getItem("studio.theme")).toBe("system");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");
  });

  it("applyTheme toggles the class without touching other classes", () => {
    document.documentElement.classList.add("other");
    applyTheme("dark");
    expect(document.documentElement.className).toBe("other dark");
    applyTheme("light");
    expect(document.documentElement.className).toBe("other");
  });
});

describe("themeScript (the blocking head script)", () => {
  const run = () => new Function(themeScript())();

  it("is self-contained and uses the same storage key", () => {
    expect(themeScript()).toContain('"studio.theme"');
    expect(themeScript()).not.toMatch(/\bimport\b|\brequire\(/);
  });

  it("System with dark media sets .dark and color-scheme dark", () => {
    mockMatchMedia(true);
    run();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });

  it("a stored Light wins over dark media", () => {
    mockMatchMedia(true);
    window.localStorage.setItem("studio.theme", "light");
    document.documentElement.classList.add("dark");
    run();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");
  });

  it("a stored Dark wins over light media", () => {
    mockMatchMedia(false);
    window.localStorage.setItem("studio.theme", "dark");
    run();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("falls back to System when storage throws", () => {
    mockMatchMedia(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(run).not.toThrow();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("does not throw without matchMedia and resolves System to light", () => {
    run();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");
  });
});
