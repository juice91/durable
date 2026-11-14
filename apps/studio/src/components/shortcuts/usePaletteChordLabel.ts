"use client";

import { useSyncExternalStore } from "react";
import { isMacPlatform, paletteChordLabel } from "@/lib/shortcuts";

const subscribeNothing = () => () => {};

/** Whether this is macOS (the palette chord is ⌘K there). False on the server and during hydration, then corrected. */
export function usePaletteIsMac(): boolean {
  return useSyncExternalStore(subscribeNothing, () => isMacPlatform(), () => false);
}

/** "⌘K" on macOS, "Ctrl K" elsewhere. The server (and hydration) renders "Ctrl K", then the client corrects it. */
export function usePaletteChordLabel(): string {
  return paletteChordLabel(usePaletteIsMac());
}

/** The `aria-keyshortcuts` value for the palette chord on this platform. */
export function usePaletteKeyShortcuts(): string {
  return useSyncExternalStore(subscribeNothing, () => (isMacPlatform() ? "Meta+K" : "Control+K"), () => "Control+K");
}
