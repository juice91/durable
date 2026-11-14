// One working memory read per note, shared by every editor on the page (the chat's memory card and
// inspector, the Memory tab): concurrent loads of the same note share one request (React's
// development double effects, the panes' remount at 1024 px), a settled answer is reused for
// `FRESH_MS`, and what Studio already knows (a note it just saved or cleared, a conversation it just
// created) is primed so it is not asked again. A write the page sees (`memory_updated`), Retry and
// Reload force a fresh read.
//
// No imports on purpose: the test setup resets it between tests, and a setup import of lib/memory
// would load lib/api before a test file can mock it.

/** How long a settled answer is reused without a forced read. */
export const FRESH_MS = 30_000;

interface Entry {
  promise: Promise<unknown>;
  /** null while the request is in flight. */
  settledAt: number | null;
}

const entries = new Map<string, Entry>();

export interface SharedLoadOptions {
  /** Ignore a cached or in-flight answer and read again (a write was seen, Retry, Reload). */
  force?: boolean;
  now?: () => number;
}

/**
 * The note for `key` (`targetKeyOf` in components/memory/WorkingMemoryEditor): the in-flight request or a fresh settled answer
 * when there is one, else `load()`. A failed read is not kept (the next load tries again).
 */
export function sharedWorkingMemory<T>(key: string, load: () => Promise<T>, { force = false, now = Date.now }: SharedLoadOptions = {}): Promise<T> {
  const cached = entries.get(key);
  if (!force && cached && (cached.settledAt === null || now() - cached.settledAt < FRESH_MS)) return cached.promise as Promise<T>;
  const entry: Entry = { promise: Promise.resolve(), settledAt: null };
  entry.promise = load().then(
    (value) => {
      if (entries.get(key) === entry) entry.settledAt = now();
      return value;
    },
    (error: unknown) => {
      if (entries.get(key) === entry) entries.delete(key);
      throw error;
    },
  );
  entries.set(key, entry);
  return entry.promise as Promise<T>;
}

/** Record what the note is now (saved, cleared, or none yet for a conversation just created). */
export function primeWorkingMemory(key: string, value: unknown, now: () => number = Date.now): void {
  entries.set(key, { promise: Promise.resolve(value), settledAt: now() });
}

/** Forget every cached note (tests; a tenant switch would too). */
export function resetWorkingMemoryCache(): void {
  entries.clear();
}
