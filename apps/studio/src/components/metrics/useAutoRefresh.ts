"use client";

import { useEffect, useState } from "react";

function hidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/** A counter that goes up every `intervalMs` while `enabled` and the tab is visible. A hidden tab
 * stops the timer; when it becomes visible again the counter goes up once (catch up) and the timer
 * restarts. Use the counter as a dependency of whatever should refresh. */
export function useAutoRefresh(enabled: boolean, intervalMs: number): number {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer === null) timer = setInterval(() => setTick((t) => t + 1), intervalMs);
    };
    const stop = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
    };
    const onVisibility = () => {
      if (hidden()) {
        stop();
      } else if (timer === null) {
        setTick((t) => t + 1);
        start();
      }
    };
    if (!hidden()) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, intervalMs]);

  return tick;
}
