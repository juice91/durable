import React from "react";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConflictError } from "./api";
import { forgetRevisions, ifMatch, revisionKey, writeResource } from "./revisions";
import { useConflict } from "./useConflict";

function conflictFor(key: string, currentRevision: number, current: unknown): ConflictError {
  const error = new ConflictError("changed", currentRevision, current);
  error.key = key;
  return error;
}

describe("useConflict", () => {
  beforeEach(() => forgetRevisions());

  it("holds a 412 as a conflict instead of throwing, and a success clears it", async () => {
    const { result } = renderHook(() => useConflict<{ id: string }>());
    await act(async () => {
      expect(await result.current.guard(async () => { throw conflictFor("agent:a1", 4, { id: "a1" }); })).toBeUndefined();
    });
    expect(result.current.conflict?.currentRevision).toBe(4);
    await act(async () => {
      expect(await result.current.guard(async () => "saved")).toBe("saved");
    });
    expect(result.current.conflict).toBeNull();
  });

  it("other errors still propagate to the caller", async () => {
    const { result } = renderHook(() => useConflict());
    await act(async () => {
      await expect(result.current.guard(async () => { throw new Error("boom"); })).rejects.toThrow("boom");
    });
    expect(result.current.conflict).toBeNull();
  });

  it("reload hands back the current resource and moves the remembered revision to it", async () => {
    const { result } = renderHook(() => useConflict<{ id: string }>());
    await act(async () => {
      await result.current.guard(async () => { throw conflictFor("agent:a1", 4, { id: "a1" }); });
    });
    let current: { id: string } | undefined;
    act(() => { current = result.current.reload(); });
    expect(current).toEqual({ id: "a1" });
    expect(result.current.conflict).toBeNull();
    expect(ifMatch("agent:a1")).toEqual({ "If-Match": '"4"' });
  });

  it("overwrite resends the same save with the current revision and reports the result", async () => {
    revisionKey.agent("a1");
    const saved = vi.fn();
    const sent: Array<Record<string, string>> = [];
    const save = async () =>
      writeResource("agent:a1", async (headers) => {
        sent.push(headers);
        if (sent.length === 1) throw conflictFor("agent:a1", 6, { id: "a1" });
        return { id: "a1", revision: 7 };
      });
    const { result } = renderHook(() => useConflict({ onSaved: saved }));
    await act(async () => { await result.current.guard(save); });
    await act(async () => { await result.current.overwrite(); });
    expect(sent).toEqual([{}, { "If-Match": '"6"' }]);
    expect(saved).toHaveBeenCalledWith({ id: "a1", revision: 7 });
    expect(result.current.conflict).toBeNull();
  });

  it("a second conflict during an overwrite shows the notice again", async () => {
    const { result } = renderHook(() => useConflict());
    const save = async () => { throw conflictFor("agent:a1", 9, null); };
    await act(async () => { await result.current.guard(save); });
    await act(async () => { await result.current.overwrite(); });
    expect(result.current.conflict?.currentRevision).toBe(9);
    expect(result.current.overwriting).toBe(false);
  });
});
