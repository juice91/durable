/**
 * Test helpers for the palette and shortcuts. Not imported by app code. Tests still mock
 * `next/navigation` themselves (the router's `push` is what navigation assertions spy on).
 */
import React from "react";
import { fireEvent } from "@testing-library/react";
import { FrameHarness } from "@/components/frame/frameTesting";
import { StudioKeyboard } from "./StudioKeyboard";

/** The whole frame with the palette, the global shortcuts and the help dialog, around `children`. */
export function KeyboardHarness({ children }: { children?: React.ReactNode }) {
  return (
    <FrameHarness>
      <StudioKeyboard>{children}</StudioKeyboard>
    </FrameHarness>
  );
}

/** Presses `key` on `target` (default: <body>), as a real keydown that bubbles to window. */
export function pressKey(key: string, init: KeyboardEventInit = {}, target: Element = document.body): void {
  fireEvent.keyDown(target, { key, bubbles: true, ...init });
}

/** Presses the keys one after another (`pressKeys("g", "t")`). */
export function pressKeys(...keys: string[]): void {
  for (const key of keys) pressKey(key);
}
