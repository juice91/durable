"use client";

import { useSyncExternalStore } from "react";
import { isMacPlatform, paletteChordLabel } from "@/lib/shortcuts";

const subscribeNothing = () => () => {};

/** "⌘K" on macOS, "Ctrl K" elsewhere. The server (and hydration) renders "Ctrl K", then the client corrects it. */
export function usePaletteChordLabel(): string {
  return useSyncExternalStore(subscribeNothing, () => paletteChordLabel(isMacPlatform()), () => paletteChordLabel(false));
}

/** The `aria-keyshortcuts` value for the palette chord on this platform. */
export function usePaletteKeyShortcuts(): string {
  return useSyncExternalStore(subscribeNothing, () => (isMacPlatform() ? "Meta+K" : "Control+K"), () => "Control+K");
}
