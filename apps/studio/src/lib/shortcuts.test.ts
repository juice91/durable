import { describe, expect, test } from "vitest";
import { allNavItems } from "./nav";
import { createKeySequence, displayKey, globalBindings, gKeyHint, isMacPlatform, isPaletteChord, paletteChordLabel, SEQUENCE_TIMEOUT_MS, shortcutSections } from "./shortcuts";

const bindings = [
  { id: "help", keys: ["?"] },
  { id: "create", keys: ["c"] },
  { id: "agents", keys: ["g", "a"] },
  { id: "traces", keys: ["g", "t"] },
];

describe("createKeySequence", () => {
  test("a single-key binding matches at once", () => {
    const seq = createKeySequence(bindings);
    expect(seq.feed("?", 0)).toEqual({ match: bindings[0], pending: false });
  });

  test("g then a letter within the window matches", () => {
    const seq = createKeySequence(bindings);
    expect(seq.feed("g", 0)).toEqual({ match: null, pending: true });
    expect(seq.isPending(500)).toBe(true);
    expect(seq.feed("t", 999)).toEqual({ match: bindings[3], pending: false });
    expect(seq.isPending(999)).toBe(false);
  });

  test("the window is 1 s by default; a late letter starts over", () => {
    const seq = createKeySequence(bindings);
    seq.feed("g", 0);
    expect(seq.isPending(SEQUENCE_TIMEOUT_MS + 1)).toBe(false);
    expect(seq.feed("a", SEQUENCE_TIMEOUT_MS + 1)).toEqual({ match: null, pending: false });
  });

  test("a key that breaks a sequence is dropped, not run alone (g then c does not create)", () => {
    const seq = createKeySequence(bindings);
    seq.feed("g", 0);
    expect(seq.feed("c", 10)).toEqual({ match: null, pending: false });
    expect(seq.feed("c", 20).match?.id).toBe("create");
  });

  test("reset forgets a pending prefix; keys are case-sensitive", () => {
    const seq = createKeySequence(bindings);
    seq.feed("g", 0);
    seq.reset();
    expect(seq.feed("a", 10).match).toBeNull();
    expect(seq.feed("G", 20).pending).toBe(false);
    expect(seq.feed("C", 30).match).toBeNull();
  });
});

describe("global bindings", () => {
  test("one g sequence per registry g-key, plus ? and c", () => {
    const result = globalBindings();
    const withG = allNavItems.filter((item) => item.gKey);
    expect(result.map((binding) => binding.id)).toEqual(["help", "create", ...withG.map((item) => `nav:${item.id}`)]);
    for (const item of withG) {
      const binding = result.find((candidate) => candidate.id === `nav:${item.id}`);
      expect(binding?.keys).toEqual(["g", item.gKey]);
      expect(binding?.action).toEqual({ kind: "navigate", item });
    }
    expect(result.some((binding) => binding.keys.includes("/") || binding.keys.includes("["))).toBe(false);
  });

  test("isPaletteChord on macOS: Cmd+K only (Ctrl+K stays delete-to-end-of-line)", () => {
    const base = { key: "k", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false };
    expect(isPaletteChord({ ...base, metaKey: true }, true)).toBe(true);
    expect(isPaletteChord({ ...base, ctrlKey: true }, true)).toBe(false);
    expect(isPaletteChord({ ...base, ctrlKey: true, metaKey: true }, true)).toBe(false);
  });

  test("isPaletteChord falls back to the key position for non-Latin layouts only", () => {
    const base = { metaKey: false, ctrlKey: true, altKey: false, shiftKey: false };
    expect(isPaletteChord({ ...base, key: "л", code: "KeyK" }, false)).toBe(true);
    // Dvorak types "t" on the KeyK position: that is Ctrl+T, not the palette.
    expect(isPaletteChord({ ...base, key: "t", code: "KeyK" }, false)).toBe(false);
    expect(isPaletteChord({ ...base, key: "k", code: "KeyV" }, false)).toBe(true);
  });

  test("platform detection and labels", () => {
    expect(isMacPlatform({ platform: "MacIntel", userAgent: "" })).toBe(true);
    expect(isMacPlatform({ platform: "Win32", userAgent: "" })).toBe(false);
    expect(isMacPlatform({ platform: "", userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" })).toBe(true);
    expect(isMacPlatform(undefined)).toBe(false);
    expect(paletteChordLabel(true)).toBe("⌘K");
    expect(paletteChordLabel(false)).toBe("Ctrl K");
  });

  test("isPaletteChord: off macOS: Ctrl+K (and Cmd+K)", () => {
    const base = { key: "k", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false };
    expect(isPaletteChord({ ...base, metaKey: true }, false)).toBe(true);
    expect(isPaletteChord({ ...base, ctrlKey: true, key: "K" }, false)).toBe(true);
    expect(isPaletteChord(base)).toBe(false);
    expect(isPaletteChord({ ...base, metaKey: true, shiftKey: true })).toBe(false);
    expect(isPaletteChord({ ...base, ctrlKey: true, altKey: true })).toBe(false);
    expect(isPaletteChord({ ...base, metaKey: true, key: "j" })).toBe(false);
  });
});

describe("createKeySequence.wouldBreak", () => {
  test("true only for a key that neither completes nor continues a pending sequence; never changes state", () => {
    const seq = createKeySequence([{ id: "gt", keys: ["g", "t"] }, { id: "c", keys: ["c"] }]);
    expect(seq.wouldBreak("[", 0)).toBe(false);
    seq.feed("g", 0);
    expect(seq.wouldBreak("[", 10)).toBe(true);
    expect(seq.wouldBreak("t", 10)).toBe(false);
    expect(seq.wouldBreak("[", 5000)).toBe(false);
    expect(seq.feed("t", 20).match?.id).toBe("gt");
  });
});

describe("shortcut sections", () => {
  test("General lists the frame and list keys; Go to lists every g-key in registry order", () => {
    const [general, navigation] = shortcutSections();
    expect(general.rows.map((row) => row.id)).toEqual(["palette", "help", "sidebar", "search", "create", "rows", "rows-ends"]);
    expect(general.rows.find((row) => row.id === "rows-ends")?.keys).toEqual([["Home"], ["End"]]);
    expect(navigation.rows.map((row) => row.label)).toEqual(allNavItems.filter((item) => item.gKey).map((item) => item.label));
    expect(navigation.rows.every((row) => row.sequence)).toBe(true);
    expect(navigation.rows.find((row) => row.id === "nav:traces")?.keys).toEqual([["G", "T"]]);
  });

  test("the palette row shows only the chord that works on this platform (⌘K on macOS, Ctrl K elsewhere)", () => {
    const palette = (mac: boolean) => shortcutSections(undefined, mac)[0].rows.find((row) => row.id === "palette")?.keys;
    expect(palette(true)).toEqual([["⌘", "K"]]);
    expect(palette(false)).toEqual([["Ctrl", "K"]]);
  });

  test("Agent page lists `]` (Config) and `{` (conversations)", () => {
    const agent = shortcutSections().find((section) => section.id === "agent");
    expect(agent?.rows.map((row) => [row.id, row.keys])).toEqual([
      ["agent-config", [["]"]]],
      ["agent-threads", [["{"]]],
    ]);
  });

  test("display helpers", () => {
    expect(displayKey("a")).toBe("A");
    expect(displayKey(",")).toBe(",");
    expect(gKeyHint({ gKey: "n" })).toBe("G N");
    expect(gKeyHint({})).toBeNull();
  });
});
