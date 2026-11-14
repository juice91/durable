import { allNavItems, type NavItem } from "./nav";

/**
 * Global keyboard shortcuts (decision "Keyboard shortcuts").
 *
 * Bound by `components/shortcuts/ShortcutsProvider` through the engine below: `?` (help), `c` (the
 * page's create action) and `g` + a letter from the nav registry. Bound elsewhere, and only listed
 * here for the help table: ⌘K / Ctrl+K (the palette), `[` (the sidebar, in the frame), `/` (the
 * DataList search binds it itself), the DataList row keys, and the agent page's `]` (Config) and
 * `{` (conversations), bound by `AgentLayout`.
 *
 * Every single-key shortcut is ignored while focus is in a field and while a dialog or menu is
 * open (`shouldHandleShortcut` in components/frame/keyboard.ts).
 */

/** The prefix of the navigation sequences: `g` then a letter. */
export const G_PREFIX = "g";
/** How long the engine waits for the next key of a sequence. */
export const SEQUENCE_TIMEOUT_MS = 1000;

export interface KeyBinding {
  id: string;
  /** `event.key` values in order, case-sensitive: `["?"]`, `["g", "a"]`. */
  keys: readonly string[];
}

export interface SequenceResult<B extends KeyBinding = KeyBinding> {
  /** The binding this key completed, or null. */
  match: B | null;
  /** The key started or continued a sequence and the engine waits for more. */
  pending: boolean;
}

export interface KeySequence<B extends KeyBinding = KeyBinding> {
  /** Feed one key press (`event.key`) at time `now` (ms). */
  feed: (key: string, now: number) => SequenceResult<B>;
  /** Forget a half-typed sequence (focus moved into a field, a dialog opened). */
  reset: () => void;
  /** True while a sequence is half-typed and not yet expired at `now`. */
  isPending: (now: number) => boolean;
  /**
   * True when `key` at `now` would break a half-typed sequence (neither completing nor continuing
   * it). Changes nothing.
   */
  wouldBreak: (key: string, now: number) => boolean;
}

/**
 * A key-sequence matcher. Each key extends the pending sequence when it arrives within
 * `timeoutMs` of the previous one; otherwise it starts a new one. A complete binding matches and
 * resets; a prefix of some binding waits; anything else resets. A key that breaks a pending
 * sequence is dropped rather than tried alone, so `g` then `c` does not run `c`'s action.
 */
export function createKeySequence<B extends KeyBinding>(bindings: readonly B[], timeoutMs: number = SEQUENCE_TIMEOUT_MS): KeySequence<B> {
  let buffer: string[] = [];
  let lastAt = 0;

  const startsWith = (keys: readonly string[], prefix: readonly string[]) =>
    prefix.length <= keys.length && prefix.every((key, index) => keys[index] === key);

  return {
    feed(key, now) {
      const continuing = buffer.length > 0 && now - lastAt <= timeoutMs;
      const candidate = continuing ? [...buffer, key] : [key];
      lastAt = now;
      const match = bindings.find((binding) => binding.keys.length === candidate.length && startsWith(binding.keys, candidate)) ?? null;
      if (match) {
        buffer = [];
        return { match, pending: false };
      }
      if (bindings.some((binding) => binding.keys.length > candidate.length && startsWith(binding.keys, candidate))) {
        buffer = candidate;
        return { match: null, pending: true };
      }
      buffer = [];
      return { match: null, pending: false };
    },
    reset() {
      buffer = [];
    },
    isPending(now) {
      return buffer.length > 0 && now - lastAt <= timeoutMs;
    },
    wouldBreak(key, now) {
      if (!(buffer.length > 0 && now - lastAt <= timeoutMs)) return false;
      const candidate = [...buffer, key];
      return !bindings.some((binding) => binding.keys.length >= candidate.length && startsWith(binding.keys, candidate));
    },
  };
}

export type GlobalShortcutAction = { kind: "help" } | { kind: "create" } | { kind: "navigate"; item: NavItem };

export interface GlobalBinding extends KeyBinding {
  action: GlobalShortcutAction;
}

/** The bindings the provider listens for: `?`, `c` and one `g` sequence per registry g-key. */
export function globalBindings(items: readonly NavItem[] = allNavItems): GlobalBinding[] {
  return [
    { id: "help", keys: ["?"], action: { kind: "help" } },
    { id: "create", keys: ["c"], action: { kind: "create" } },
    ...items
      .filter((item): item is NavItem & { gKey: string } => typeof item.gKey === "string" && item.gKey.length > 0)
      .map((item): GlobalBinding => ({ id: `nav:${item.id}`, keys: [G_PREFIX, item.gKey], action: { kind: "navigate", item } })),
  ];
}

/** True on macOS (and iPadOS with a keyboard). False on the server and where the platform is unknown. */
export function isMacPlatform(nav: Pick<Navigator, "platform" | "userAgent"> | undefined = typeof navigator === "undefined" ? undefined : navigator): boolean {
  if (!nav) return false;
  const data = (nav as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  const platform = data?.platform || nav.platform || "";
  return /mac|iphone|ipad|ipod/i.test(platform) || (!platform && /mac os x/i.test(nav.userAgent ?? ""));
}

export type PaletteChordEvent = Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey"> & { code?: string };

/**
 * The palette chord. On macOS only ⌘K: Ctrl+K stays "delete to the end of the line" in text fields.
 * Elsewhere Ctrl+K (⌘K, rarely reachable there, is accepted too). Shift and Alt variants are not.
 * The key is matched by its character, or by its position (`code` "KeyK") when the layout types a
 * non-Latin character there; a Latin layout that puts another letter on that key is not matched.
 */
export function isPaletteChord(event: PaletteChordEvent, mac: boolean = isMacPlatform()): boolean {
  if (event.altKey || event.shiftKey) return false;
  if (mac ? !event.metaKey || event.ctrlKey : !(event.ctrlKey || event.metaKey)) return false;
  const key = event.key ?? "";
  if (key.toLowerCase() === "k") return true;
  return event.code === "KeyK" && !/^[a-z]$/i.test(key);
}

/** How the palette chord is written for this platform: "⌘K" or "Ctrl K". */
export function paletteChordLabel(mac: boolean = isMacPlatform()): string {
  return paletteChordKeys(mac).join(mac ? "" : " ");
}

/** The palette chord's keys as `<Kbd>`s on this platform: ⌘ K on macOS (Ctrl+K does not open it there), Ctrl K elsewhere. */
export function paletteChordKeys(mac: boolean = isMacPlatform()): readonly string[] {
  return mac ? ["⌘", "K"] : ["Ctrl", "K"];
}

export interface ShortcutRow {
  id: string;
  label: string;
  /** Alternatives (⌘K or Ctrl K); each is a list of keys shown as `<Kbd>`s. */
  keys: readonly (readonly string[])[];
  /** The keys are pressed one after another ("G then A"), not together. */
  sequence?: boolean;
}

export interface ShortcutSection {
  id: string;
  title: string;
  rows: ShortcutRow[];
}

/** How a g-key is shown: upper case for letters, the character itself otherwise. */
export function displayKey(key: string): string {
  return key.length === 1 ? key.toUpperCase() : key;
}

/**
 * The rows of the help dialog and the Settings table, in display order. `mac` picks the palette
 * chord shown (only the one that works on this platform; components pass `usePaletteIsMac()`).
 */
export function shortcutSections(items: readonly NavItem[] = allNavItems, mac: boolean = isMacPlatform()): ShortcutSection[] {
  return [
    {
      id: "general",
      title: "General",
      rows: [
        { id: "palette", label: "Open the command palette", keys: [paletteChordKeys(mac)] },
        { id: "help", label: "Show keyboard shortcuts", keys: [["?"]] },
        { id: "sidebar", label: "Collapse or expand the sidebar", keys: [["["]] },
        { id: "search", label: "Focus the list search", keys: [["/"]] },
        { id: "create", label: "Create (the page's primary action)", keys: [["C"]] },
        { id: "rows", label: "Move through list rows, then open one", keys: [["↑"], ["↓"], ["Enter"]] },
        { id: "rows-ends", label: "Jump to the first or last list row", keys: [["Home"], ["End"]] },
      ],
    },
    {
      id: "navigation",
      title: "Go to",
      rows: items
        .filter((item) => item.gKey)
        .map((item) => ({ id: `nav:${item.id}`, label: item.label, keys: [[displayKey(G_PREFIX), displayKey(item.gKey as string)]], sequence: true })),
    },
    {
      id: "agent",
      title: "Agent page",
      rows: [
        { id: "agent-config", label: "Show or hide the Config panel", keys: [["]"]] },
        { id: "agent-threads", label: "Show or hide the conversations pane (Chat tab)", keys: [["{"]] },
      ],
    },
  ];
}

/** The `<Kbd>` text of a g-key sequence ("G A"), for palette rows and tooltips. */
export function gKeyHint(item: Pick<NavItem, "gKey">): string | null {
  return item.gKey ? `${displayKey(G_PREFIX)} ${displayKey(item.gKey)}` : null;
}
