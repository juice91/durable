"use client";

import { useUnsavedChangesGuard } from "@/components/agents/stepper/useUnsavedChangesGuard";
import { WORKING_MEMORY_GUARD_COPY } from "./memoryModel";

/**
 * Mounted only while a working memory edit is unsaved: closing or reloading the tab, a link to
 * another page and navigation by code (`g` keys, the palette, the agent switcher) ask first
 * (components/agents/stepper/useUnsavedChangesGuard, with the memory copy).
 */
export function WorkingMemoryGuard() {
  return <>{useUnsavedChangesGuard(true, WORKING_MEMORY_GUARD_COPY)}</>;
}
