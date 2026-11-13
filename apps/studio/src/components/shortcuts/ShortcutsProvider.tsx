"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { shouldHandleShortcut } from "@/components/frame/keyboard";
import { createKeySequence, globalBindings, type GlobalBinding } from "@/lib/shortcuts";
import { ShortcutHelpDialog } from "./ShortcutHelpDialog";

/**
 * A page's create button can opt in to `c` without code: the first enabled element inside <main>
 * with this attribute is clicked when no `useCreateAction` handler is registered.
 */
export const CREATE_ACTION_ATTRIBUTE = "data-shortcut-create";

export interface ShortcutsContextValue {
  helpOpen: boolean;
  openHelp: () => void;
  setHelpOpen: (open: boolean) => void;
  /** Registers the page's primary create action for `c`; the latest registration wins. Returns the unregister function. */
  registerCreateAction: (action: () => void) => () => void;
}

const ShortcutsContext = createContext<ShortcutsContextValue | null>(null);

/** The shortcut state, or null outside the provider (a page rendered alone in a test). */
export function useShortcuts(): ShortcutsContextValue | null {
  return useContext(ShortcutsContext);
}

/**
 * Registers `action` as the page's `c` shortcut while the component is mounted. Pass null to
 * register nothing (an action that is not available right now). Outside the provider it does
 * nothing. The latest `action` is always the one called, so an inline arrow is fine.
 */
export function useCreateAction(action: (() => void) | null): void {
  const register = useContext(ShortcutsContext)?.registerCreateAction;
  const ref = useRef(action);
  ref.current = action;
  const enabled = action !== null;
  useEffect(() => {
    if (!register || !enabled) return;
    return register(() => ref.current?.());
  }, [register, enabled]);
}

function clickDeclaredCreate(): boolean {
  const candidates = Array.from(document.querySelectorAll<HTMLElement>(`main [${CREATE_ACTION_ATTRIBUTE}]`));
  const target = candidates.find((element) => !element.matches(":disabled, [aria-disabled='true']"));
  if (!target) return false;
  target.click();
  return true;
}

/**
 * Listens for the global single-key shortcuts and owns the help dialog. `?` opens help, `c` runs the
 * page's create action, `g` then a letter (within 1 s) opens a registry page. Keys typed in fields,
 * with modifiers, or while any dialog or menu is open are ignored (`shouldHandleShortcut`).
 */
export function ShortcutsProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [helpOpen, setHelpOpen] = useState(false);
  const createActions = useRef<Array<{ run: () => void }>>([]);
  const routerRef = useRef(router);
  routerRef.current = router;

  const registerCreateAction = useCallback((action: () => void) => {
    const entry = { run: action };
    createActions.current.push(entry);
    return () => {
      createActions.current = createActions.current.filter((candidate) => candidate !== entry);
    };
  }, []);

  const openHelp = useCallback(() => setHelpOpen(true), []);

  useEffect(() => {
    const sequence = createKeySequence<GlobalBinding>(globalBindings());
    function run(binding: GlobalBinding) {
      const action = binding.action;
      if (action.kind === "help") setHelpOpen(true);
      else if (action.kind === "navigate") routerRef.current.push(action.item.href);
      else {
        const registered = createActions.current[createActions.current.length - 1];
        if (registered) registered.run();
        else clickDeclaredCreate();
      }
    }
    function onKeyDown(event: KeyboardEvent) {
      if (!shouldHandleShortcut(event)) {
        // A key typed into a field (or under a dialog) also breaks a half-typed `g` sequence.
        if (event.key !== "Shift") sequence.reset();
        return;
      }
      // Shift alone (on the way to `?`) neither breaks nor starts a sequence.
      if (event.key === "Shift" || event.key === "Control" || event.key === "Meta" || event.key === "Alt") return;
      const result = sequence.feed(event.key, Date.now());
      if (result.match) {
        event.preventDefault();
        run(result.match);
      } else if (result.pending) {
        event.preventDefault();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const value = useMemo<ShortcutsContextValue>(() => ({ helpOpen, openHelp, setHelpOpen, registerCreateAction }), [helpOpen, openHelp, registerCreateAction]);

  return (
    <ShortcutsContext.Provider value={value}>
      {children}
      <ShortcutHelpDialog open={helpOpen} onOpenChange={setHelpOpen} />
    </ShortcutsContext.Provider>
  );
}
