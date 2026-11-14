/**
 * Test helpers for the versions list's row menus (SL.11). Not imported by app code.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect } from "vitest";

function triggerName(version: number | "draft"): string {
  return version === "draft" ? "Actions for the draft" : `Actions for version ${version}`;
}

async function openMenu(version: number | "draft"): Promise<HTMLElement> {
  const trigger = await screen.findByRole("button", { name: triggerName(version) });
  trigger.focus();
  // Radix opens its menu on pointerdown or a key; the keyboard path works in jsdom.
  fireEvent.keyDown(trigger, { key: "Enter" });
  return screen.findByRole("menu");
}

/** Opens version `version`'s row menu and chooses `action` ("Restore this version", "Compare with draft"). */
export async function chooseVersionAction(version: number | "draft", action: string | RegExp): Promise<void> {
  const menu = await openMenu(version);
  fireEvent.click(within(menu).getByRole("menuitem", { name: action }));
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
}

/** The names of the items in version `version`'s row menu (the menu is closed again afterwards). */
export async function versionActions(version: number | "draft"): Promise<string[]> {
  const menu = await openMenu(version);
  const names = within(menu)
    .getAllByRole("menuitem")
    .map((item) => item.textContent?.trim() ?? "");
  fireEvent.keyDown(menu, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  return names;
}
