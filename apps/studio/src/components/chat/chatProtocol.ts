"use client";

import { useCallback, useEffect, useState } from "react";
import { readStorage, writeStorage } from "@/lib/safeStorage";

// The agent Chat tab's protocol (AGUI.12, D21, Q6): "agui" streams the chat through the AG-UI adapter
// (`useAgUiRuntime` + `ResumableHttpAgent`); "native" keeps the native runtime (`thread.ts` over
// `/runs/{id}/stream`) as the fallback for one milestone. Settings shows the switch; the default is
// AG-UI. Every other Studio screen stays native whatever this says (D20).

export type ChatProtocol = "agui" | "native";

/** localStorage key (registered in Settings → Data in this browser). */
export const CHAT_PROTOCOL_STORAGE_KEY = "studio.chat.protocol";

export const CHAT_PROTOCOL_DEFAULT: ChatProtocol = "agui";

/** Fired on `window` when this tab changes the protocol (the `storage` event covers other tabs). */
export const CHAT_PROTOCOL_EVENT = "studio:chat-protocol";

let defaultProtocol: ChatProtocol = CHAT_PROTOCOL_DEFAULT;

/**
 * Test harness only: the protocol used when nothing is stored. The suites written before AGUI.12
 * mock the native routes, so the shared test setup sets "native"; AG-UI suites store "agui" (a stored
 * value always wins) or call this.
 */
export function setChatProtocolDefaultForTests(protocol: ChatProtocol): void {
  defaultProtocol = protocol;
}

function parse(value: string | null): ChatProtocol | null {
  return value === "agui" || value === "native" ? value : null;
}

/** The stored protocol, else the default (AG-UI). Never throws. */
export function readChatProtocol(): ChatProtocol {
  return parse(readStorage(CHAT_PROTOCOL_STORAGE_KEY)) ?? defaultProtocol;
}

/** Stores the protocol and tells this tab's open chats. False when storage is unavailable. */
export function writeChatProtocol(protocol: ChatProtocol): boolean {
  const stored = writeStorage(CHAT_PROTOCOL_STORAGE_KEY, protocol);
  if (typeof window !== "undefined") {
    try {
      window.dispatchEvent(new CustomEvent(CHAT_PROTOCOL_EVENT, { detail: protocol }));
    } catch {
      // CustomEvent missing (very old engines): the next mount reads storage.
    }
  }
  return stored;
}

/** The protocol, following changes from Settings (this tab) and from other tabs. */
export function useChatProtocol(): [ChatProtocol, (protocol: ChatProtocol) => void] {
  const [protocol, setProtocol] = useState<ChatProtocol>(() => readChatProtocol());
  useEffect(() => {
    const sync = () => setProtocol(readChatProtocol());
    sync();
    window.addEventListener(CHAT_PROTOCOL_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CHAT_PROTOCOL_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  const update = useCallback((next: ChatProtocol) => {
    writeChatProtocol(next);
    setProtocol(next);
  }, []);
  return [protocol, update];
}
