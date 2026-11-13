import { act, fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme } from "./ThemeProvider";
import { renderToStaticMarkup } from "react-dom/server";
import { ThemeScript } from "./ThemeScript";

type Listener = (event: { matches: boolean }) => void;

function installMatchMedia(initialDark: boolean) {
  const listeners = new Set<Listener>();
  const mql = {
    matches: initialDark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_type: string, fn: Listener) => listeners.add(fn),
    removeEventListener: (_type: string, fn: Listener) => listeners.delete(fn),
  };
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: vi.fn(() => mql) });
  return {
    listeners,
    setDark(dark: boolean) {
      mql.matches = dark;
      act(() => listeners.forEach((fn) => fn({ matches: dark })));
    },
  };
}

function Probe() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="pref">{theme}</span>
      <span data-testid="resolved">{resolvedTheme}</span>
      <button onClick={() => setTheme("light")}>light</button>
      <button onClick={() => setTheme("dark")}>dark</button>
      <button onClick={() => setTheme("system")}>system</button>
    </div>
  );
}

const root = document.documentElement;

beforeEach(() => {
  window.localStorage.clear();
  root.className = "";
  root.removeAttribute("style");
});

afterEach(() => {
  // @ts-expect-error -- jsdom has no matchMedia; tests add it per case
  delete window.matchMedia;
  vi.restoreAllMocks();
});

describe("ThemeProvider", () => {
  it("follows the OS on System, live", () => {
    const media = installMatchMedia(true);
    render(<ThemeProvider><Probe /></ThemeProvider>);
    expect(screen.getByTestId("pref")).toHaveTextContent("system");
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
    expect(root.classList.contains("dark")).toBe(true);

    media.setDark(false);
    expect(root.classList.contains("dark")).toBe(false);
    expect(root.style.colorScheme).toBe("light");
    media.setDark(true);
    expect(root.classList.contains("dark")).toBe(true);
  });

  it("ignores OS changes while an explicit theme is chosen", () => {
    const media = installMatchMedia(false);
    window.localStorage.setItem("studio.theme", "light");
    render(<ThemeProvider><Probe /></ThemeProvider>);
    media.setDark(true);
    expect(root.classList.contains("dark")).toBe(false);
    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
  });

  it("setTheme changes the class and storage", () => {
    installMatchMedia(false);
    render(<ThemeProvider><Probe /></ThemeProvider>);
    fireEvent.click(screen.getByText("dark"));
    expect(root.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("studio.theme")).toBe("dark");
    fireEvent.click(screen.getByText("system"));
    expect(root.classList.contains("dark")).toBe(false);
    expect(window.localStorage.getItem("studio.theme")).toBe("system");
  });

  it("does not overwrite the head script's dark class on mount", () => {
    installMatchMedia(false);
    window.localStorage.setItem("studio.theme", "dark");
    root.classList.add("dark");
    render(<ThemeProvider><Probe /></ThemeProvider>);
    expect(root.classList.contains("dark")).toBe(true);
  });

  it("follows a change made in another tab", () => {
    installMatchMedia(false);
    render(<ThemeProvider><Probe /></ThemeProvider>);
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "studio.theme", newValue: "dark" }));
    });
    expect(screen.getByTestId("pref")).toHaveTextContent("dark");
    expect(root.classList.contains("dark")).toBe(true);
  });

  it("falls back to System when storage throws", () => {
    installMatchMedia(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<ThemeProvider><Probe /></ThemeProvider>);
    expect(screen.getByTestId("pref")).toHaveTextContent("system");
    expect(root.classList.contains("dark")).toBe(true);
    fireEvent.click(screen.getByText("light"));
    expect(root.classList.contains("dark")).toBe(false);
  });

  it("works without matchMedia (System resolves to light)", () => {
    render(<ThemeProvider><Probe /></ThemeProvider>);
    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
  });

  it("useTheme outside the provider throws a clear error", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Probe />)).toThrow("useTheme must be used inside <ThemeProvider>");
  });
});

describe("ThemeScript", () => {
  it("server-renders the inline head script", () => {
    const html = renderToStaticMarkup(<ThemeScript />);
    expect(html).toMatch(/^<script id="studio-theme-script">\(function\(\)\{/);
    expect(html).toContain("studio.theme");
  });
});

describe("ThemeProvider first render", () => {
  it("reports the head script's theme before the stored preference is read", () => {
    installMatchMedia(false);
    window.localStorage.setItem("studio.theme", "dark");
    root.classList.add("dark");
    const seen: string[] = [];
    function Recorder() {
      seen.push(useTheme().resolvedTheme);
      return null;
    }
    render(<ThemeProvider><Recorder /></ThemeProvider>);
    expect(seen[0]).toBe("dark");
    expect(seen).not.toContain("light");
  });
});
