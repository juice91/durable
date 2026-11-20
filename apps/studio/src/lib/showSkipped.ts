"use client";

import { useCallback, useEffect, useState } from "react";
import { readStorage, writeStorage } from "./safeStorage";

/** Whether run lists (Traces, a workflow's run history) also list `skipped` runs: off by default. */
export const RUNS_SHOW_SKIPPED_KEY = "studio.runs.show-skipped";

export function readShowSkipped(): boolean {
  return readStorage(RUNS_SHOW_SKIPPED_KEY) === "true";
}

/** The toggle's state, remembered in this browser. Read after mount so server and client markup agree. */
export function useShowSkipped(): [boolean, (next: boolean) => void] {
  const [show, setShow] = useState(false);
  useEffect(() => setShow(readShowSkipped()), []);
  const update = useCallback((next: boolean) => {
    setShow(next);
    writeStorage(RUNS_SHOW_SKIPPED_KEY, String(next));
  }, []);
  return [show, update];
}

/** `runs` without the skipped ones unless `show`, and how many skipped runs there are. */
export function splitSkipped<T extends { display_status?: string; status?: string }>(
  runs: readonly T[],
  show: boolean,
): { visible: T[]; skippedCount: number } {
  const isSkipped = (run: T) => (run.display_status ?? run.status) === "skipped";
  const skippedCount = runs.filter(isSkipped).length;
  return { visible: show ? [...runs] : runs.filter((run) => !isSkipped(run)), skippedCount };
}
