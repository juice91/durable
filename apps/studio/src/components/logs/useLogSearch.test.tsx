import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { searchLogs, type LogItem, type LogsQueryParams, type LogsResponse } from "./logsQuery";
import { useLogSearch } from "./useLogSearch";

vi.mock("./logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./logsQuery")>()),
  searchLogs: vi.fn(),
}));

function item(ts: string, message: string): LogItem {
  return { ts, level: "info", message, service: "eap-api", trace_id: null, span_id: null, run_id: null };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const page = (items: LogItem[], limit = 2): LogsResponse => ({ items, from: "2026-11-13T09:00:00+00:00", to: "2026-11-13T10:00:00+00:00", limit });

describe("useLogSearch", () => {
  beforeEach(() => {
    vi.mocked(searchLogs).mockReset();
  });

  it("ignores a slow response for a previous key", async () => {
    const first = deferred<LogsResponse>();
    const second = deferred<LogsResponse>();
    vi.mocked(searchLogs).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook(({ k }) => useLogSearch(k, () => ({ q: k, limit: 2 })), { initialProps: { k: "a" } });
    rerender({ k: "b" });
    await act(async () => second.resolve(page([item("2026-11-13T09:30:00+00:00", "new")])));
    await act(async () => first.resolve(page([item("2026-11-13T09:31:00+00:00", "old")])));
    expect(result.current.items.map((i) => i.message)).toEqual(["new"]);
    expect(vi.mocked(searchLogs).mock.calls[0][1]?.aborted).toBe(true);
  });

  it("ignores a late error for a previous key", async () => {
    const first = deferred<LogsResponse>();
    vi.mocked(searchLogs).mockReturnValueOnce(first.promise).mockResolvedValueOnce(page([]));
    const { result, rerender } = renderHook(({ k }) => useLogSearch(k, () => ({ limit: 2 })), { initialProps: { k: "a" } });
    rerender({ k: "b" });
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => first.reject(new ApiError(503, "Log search is unavailable")));
    expect(result.current.error).toBeNull();
  });

  it("loads older records with a cursor from the oldest one and the original window start", async () => {
    vi.mocked(searchLogs)
      .mockResolvedValueOnce(page([item("2026-11-13T09:50:00.000002+00:00", "b"), item("2026-11-13T09:40:00+00:00", "a")]))
      .mockResolvedValueOnce(page([item("2026-11-13T09:40:00+00:00", "a"), item("2026-11-13T09:30:00+00:00", "z")]));
    const { result } = renderHook(() => useLogSearch("k", () => ({ q: "x", limit: 2 })));
    await waitFor(() => expect(result.current.hasOlder).toBe(true));
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    const params = vi.mocked(searchLogs).mock.calls[1][0] as LogsQueryParams;
    expect(params).toEqual({ q: "x", limit: 2, from: "2026-11-13T09:00:00+00:00", to: "2026-11-13T09:40:00.000001Z" });
    expect(result.current.items.map((i) => i.message)).toEqual(["b", "a", "z"]);
    expect(result.current.hasOlder).toBe(true);
  });

  it("stops offering older records when a page adds nothing new or is not full", async () => {
    const a = item("2026-11-13T09:40:00+00:00", "a");
    const b = item("2026-11-13T09:40:00+00:00", "b");
    vi.mocked(searchLogs).mockResolvedValueOnce(page([a, b])).mockResolvedValueOnce(page([a, b]));
    const { result } = renderHook(() => useLogSearch("k", () => ({ limit: 2 })));
    await waitFor(() => expect(result.current.hasOlder).toBe(true));
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.loadingOlder).toBe(false));
    expect(result.current.hasOlder).toBe(false);
    expect(result.current.items).toHaveLength(2);
  });

  it("drops an older page that arrives after the key changed (stale cursor)", async () => {
    const older = deferred<LogsResponse>();
    vi.mocked(searchLogs)
      .mockResolvedValueOnce(page([item("2026-11-13T09:50:00+00:00", "a1"), item("2026-11-13T09:49:00+00:00", "a2")]))
      .mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce(page([item("2026-11-13T09:55:00+00:00", "b1")]));
    const { result, rerender } = renderHook(({ k }) => useLogSearch(k, () => ({ limit: 2 })), { initialProps: { k: "a" } });
    await waitFor(() => expect(result.current.hasOlder).toBe(true));
    act(() => result.current.loadOlder());
    rerender({ k: "b" });
    await waitFor(() => expect(result.current.items.map((i) => i.message)).toEqual(["b1"]));
    await act(async () => older.resolve(page([item("2026-11-13T09:48:00+00:00", "a3")])));
    expect(result.current.items.map((i) => i.message)).toEqual(["b1"]);
    expect(result.current.loadingOlder).toBe(false);
    expect(result.current.hasOlder).toBe(false);
  });

  it("reports the unavailable state and sends nothing for a null key", async () => {
    vi.mocked(searchLogs).mockRejectedValueOnce(new ApiError(503, "Log search is not configured"));
    const { result, rerender } = renderHook(({ k }: { k: string | null }) => useLogSearch(k, () => ({})), { initialProps: { k: "a" as string | null } });
    await waitFor(() => expect(result.current.error).toEqual({ kind: "unavailable", message: "Log search is not configured" }));
    rerender({ k: null });
    expect(result.current.error).toBeNull();
    expect(searchLogs).toHaveBeenCalledTimes(1);
  });

  it("keeps 'load older' while the agent filter left older runs out, on a window ending at the oldest record (review minor 7)", async () => {
    const limited = (items: LogItem[], agent_runs_limited: boolean): LogsResponse => ({ ...page(items, 5), agent_runs: 200, agent_runs_limited });
    vi.mocked(searchLogs)
      .mockResolvedValueOnce(limited([item("2026-11-13T09:50:00+00:00", "b"), item("2026-11-13T09:40:00+00:00", "a")], true))
      .mockResolvedValueOnce(limited([item("2026-11-13T09:20:00+00:00", "z")], true))
      .mockResolvedValueOnce(limited([item("2026-11-13T09:10:00+00:00", "y")], false));
    const { result } = renderHook(() => useLogSearch("k", () => ({ agent_id: "a1", limit: 5 })));
    // A short page (2 < 5) still has older records: the API searched only the agent's newest runs.
    await waitFor(() => expect(result.current.hasOlder).toBe(true));
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(vi.mocked(searchLogs).mock.calls[1][0]).toMatchObject({ agent_id: "a1", from: "2026-11-13T09:00:00+00:00", to: "2026-11-13T09:40:00.000001Z" });
    expect(result.current.hasOlder).toBe(true);
    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.items).toHaveLength(4));
    // Not limited any more, and a short page: nothing older.
    expect(result.current.hasOlder).toBe(false);
  });

  it("a short page without the limit has nothing older", async () => {
    vi.mocked(searchLogs).mockResolvedValueOnce({ ...page([item("2026-11-13T09:50:00+00:00", "b")], 5), agent_runs: 3, agent_runs_limited: false });
    const { result } = renderHook(() => useLogSearch("k", () => ({ agent_id: "a1", limit: 5 })));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasOlder).toBe(false);
  });
});
