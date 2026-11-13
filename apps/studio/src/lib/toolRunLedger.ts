// Tool test runs whose outcome the page has not seen, kept outside any component so the run id
// survives a lost response, an unmount or a reload of the tab.
//
// A run id is recorded BEFORE the request is sent, per (tool id, canonical input). The next Run of
// the same tool with the same input reuses it, so the API attaches the retry to the run it may
// already have recorded instead of executing the tool a second time. An entry is cleared only by a
// definitive answer for that key: 202 (accepted; the run is then followed or listed as a note) or a
// 4xx (nothing was recorded for that request). Transport failures, unreadable responses and 5xx
// keep it. Accepted runs the page could not show become persistent "Open run" notes.
//
// Memory is the source of truth. sessionStorage only hydrates it once (first access in the browser)
// and is written through on every change, so a failed write (quota, private mode) can never hide
// a recorded run id from the next lookup. Pending ids are never evicted; notes keep the last 20.

export type PendingState =
  /** Sent, no answer yet (or the page went away before it came). */
  | "in_flight"
  /** The request may have reached the server: network error, unreadable response, 5xx. */
  | "uncertain"
  /** 503: the run was recorded but not started; the same request starts it. */
  | "not_started";

export interface PendingRun {
  toolId: string;
  toolTitle: string;
  inputKey: string;
  runId: string;
  state: PendingState;
}

export interface StartedRunNote {
  runId: string;
  toolId: string;
  toolTitle: string;
}

interface LedgerData {
  pending: Record<string, PendingRun>;
  notes: StartedRunNote[];
}

export const MAX_NOTES = 20;

// Two seeded 53-bit string hashes (cyrb53): ~106 bits, synchronous, no input stored. A collision
// could only reuse a run id for a different input, which the API refuses with 409.
function cyrb53(text: string, seed: number): string {
  let h1 = 0xdeadbeef ^ seed;
  let h2 = 0x41c6ce57 ^ seed;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16).padStart(14, "0");
}

/**
 * A short, stable key for a tool input: a hash of its JSON with object keys sorted, so form and
 * JSON mode give the same key for the same input. The input itself is never stored.
 */
export function canonicalInputKey(value: unknown): string {
  const sort = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(sort);
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.keys(item as Record<string, unknown>).sort().map((key) => [key, sort((item as Record<string, unknown>)[key])]));
    }
    return item;
  };
  const json = JSON.stringify(sort(value)) ?? "undefined";
  return `h1:${cyrb53(json, 1)}${cyrb53(json, 2)}`;
}

/** A ledger in its own sessionStorage slot (`storageKey`); each has its own memory and listeners. */
export function createRunLedger(storageKey: string) {
  let memory: LedgerData = { pending: {}, notes: [] };
  let hydrated = false;
  const listeners = new Set<() => void>();

  function slot(toolId: string, inputKey: string): string {
    return `${toolId}\u0000${inputKey}`;
  }

  function hydrate() {
    if (hydrated || typeof window === "undefined") return;
    hydrated = true;
    try {
      const raw = window.sessionStorage.getItem(storageKey);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Partial<LedgerData>;
      memory = {
        // Anything already in memory (none on first access) wins over what storage had.
        pending: { ...(parsed.pending && typeof parsed.pending === "object" ? parsed.pending : {}), ...memory.pending },
        notes: [...(Array.isArray(parsed.notes) ? parsed.notes : []), ...memory.notes].slice(-MAX_NOTES),
      };
    } catch {
      // Unreadable or unavailable storage: start from memory.
    }
  }

  function read(): LedgerData {
    hydrate();
    return memory;
  }

  function write(data: LedgerData) {
    hydrate();
    memory = data;
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify(data));
    } catch {
      // Quota or private mode: memory stays authoritative for this page load.
    }
    listeners.forEach((listener) => listener());
  }

  return {
    pending(toolId: string, inputKey: string): PendingRun | undefined {
      return read().pending[slot(toolId, inputKey)];
    },
    /** Record (or update) the run id for this tool and input. */
    remember(entry: PendingRun) {
      const data = read();
      write({ ...data, pending: { ...data.pending, [slot(entry.toolId, entry.inputKey)]: entry } });
    },
    /** Clear this key, but only if it still holds this run id (a newer request owns it otherwise). */
    forget(toolId: string, inputKey: string, runId: string) {
      const data = read();
      const key = slot(toolId, inputKey);
      if (data.pending[key]?.runId !== runId) return;
      const pending = { ...data.pending };
      delete pending[key];
      write({ ...data, pending });
    },
    notes(): StartedRunNote[] {
      return read().notes;
    },
    addNote(note: StartedRunNote) {
      const data = read();
      if (data.notes.some((item) => item.runId === note.runId)) return;
      write({ ...data, notes: [...data.notes, note].slice(-MAX_NOTES) });
    },
    dismissNote(runId: string) {
      const data = read();
      write({ ...data, notes: data.notes.filter((item) => item.runId !== runId) });
    },
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    /** Tests only: forget memory and hydrate from sessionStorage again, as a fresh page load would. */
    reloadFromStorage() {
      memory = { pending: {}, notes: [] };
      hydrated = false;
    },
    /** Tests only. */
    reset() {
      memory = { pending: {}, notes: [] };
      hydrated = true;
      try {
        window.sessionStorage.removeItem(storageKey);
      } catch {
        // ignore
      }
      listeners.forEach((listener) => listener());
    },
  };
}

export type RunLedger = ReturnType<typeof createRunLedger>;

export const toolRunLedger = createRunLedger("eap.studio.toolRuns.v1");
