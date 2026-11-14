import { afterEach, describe, expect, test, vi } from "vitest";
import { cachedEntities, loadEntities, PALETTE_CACHE_MS, resetPaletteCache, runIdFromQuery, scopeShows, type EntitySource } from "./paletteData";

afterEach(() => resetPaletteCache());

function source(load: EntitySource["load"]): EntitySource {
  return { kind: "agents", title: "Agents", noun: "agents", testId: "command-palette-error-agents", load };
}

describe("entity cache", () => {
  test("a load is reused for 60 s, then fetched again", async () => {
    const load = vi.fn().mockResolvedValue([{ kind: "agents", id: "a1", label: "weather", href: "/x" }]);
    let now = 1_000;
    const clock = () => now;
    await loadEntities(source(load), clock);
    now += PALETTE_CACHE_MS - 1;
    await loadEntities(source(load), clock);
    expect(load).toHaveBeenCalledTimes(1);
    expect(cachedEntities("agents", now)).toHaveLength(1);
    now += 1;
    expect(cachedEntities("agents", now)).toBeNull();
    await loadEntities(source(load), clock);
    expect(load).toHaveBeenCalledTimes(2);
  });

  test("a load in flight is shared by a quick reopen", async () => {
    let resolve: (items: never[]) => void = () => {};
    const load = vi.fn(() => new Promise<never[]>((r) => (resolve = r)));
    const first = loadEntities(source(load));
    const second = loadEntities(source(load));
    resolve([]);
    await expect(Promise.all([first, second])).resolves.toEqual([[], []]);
    expect(load).toHaveBeenCalledTimes(1);
  });

  test("a failure is not cached", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("HTTP 500")).mockResolvedValueOnce([]);
    await expect(loadEntities(source(load))).rejects.toThrow("HTTP 500");
    expect(cachedEntities("agents")).toBeNull();
    await expect(loadEntities(source(load))).resolves.toEqual([]);
    expect(load).toHaveBeenCalledTimes(2);
  });
});

describe("runIdFromQuery", () => {
  test("a UUID (spaces trimmed, any case) is a run id; anything else is not", () => {
    expect(runIdFromQuery(" 0B6F3A52-1C2D-4E5F-8A9B-0C1D2E3F4A5B ")).toBe("0b6f3a52-1c2d-4e5f-8a9b-0c1d2e3f4a5b");
    expect(runIdFromQuery("0b6f3a52")).toBeNull();
    expect(runIdFromQuery("weather")).toBeNull();
    expect(runIdFromQuery("0b6f3a52-1c2d-4e5f-8a9b-0c1d2e3f4a5bx")).toBeNull();
  });
});

test("scopeShows: All shows every group; a scope shows only itself", () => {
  expect(scopeShows("all", "agents")).toBe(true);
  expect(scopeShows("agents", "agents")).toBe(true);
  expect(scopeShows("agents", "pages")).toBe(false);
});
