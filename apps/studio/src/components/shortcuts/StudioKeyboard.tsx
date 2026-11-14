"use client";

import React from "react";
import { CommandPalette } from "@/components/palette/CommandPalette";
import { ShortcutsProvider } from "./ShortcutsProvider";

/**
 * The palette, the global shortcuts and the help dialog, mounted once inside the app frame (in the
 * root layout, around the page). Needs the ThemeProvider and the frame above it.
 */
export function StudioKeyboard({ children }: { children?: React.ReactNode }) {
  return (
    <ShortcutsProvider>
      <CommandPalette />
      {children}
    </ShortcutsProvider>
  );
}
