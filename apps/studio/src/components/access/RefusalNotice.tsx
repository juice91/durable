"use client";

import React, { useEffect, useRef, useState } from "react";
import { askFor, explainAccess } from "@/lib/access";
import { onRefusal, type Refusal } from "@/lib/refusals";
import { canExplain, useCapabilities } from "@/lib/useIam";
import { Button } from "@/components/ui/Button";

const UNAVAILABLE = askFor({ action: "read", allowed: false, reason: "engine_unavailable", needed: null }, "tenant");

/**
 * The frame's "why can't I?": after a call the policy engine refused (or could not answer), the reason and what to ask an
 * admin for, until dismissed. Silent under `openaccess`, which refuses nothing.
 */
export function RefusalNotice() {
  const caps = useCapabilities();
  const [message, setMessage] = useState<string | null>(null);
  const explains = useRef(false);
  explains.current = canExplain(caps);

  useEffect(() => {
    let controller: AbortController | null = null;
    const stop = onRefusal((refusal: Refusal) => {
      controller?.abort();
      if (refusal.type === "unavailable") {
        setMessage(UNAVAILABLE);
        return;
      }
      if (!explains.current) return;
      const mine = new AbortController();
      controller = mine;
      explainAccess(refusal.kind, refusal.id, [refusal.action], mine.signal).then(
        (answer) => {
          const denied = answer.decisions.find((d) => !d.allowed);
          if (!mine.signal.aborted && denied) setMessage(askFor(denied, refusal.kind, answer.roles));
        },
        () => undefined,
      );
    });
    return () => {
      controller?.abort();
      stop();
    };
  }, []);

  if (!message) return null;
  return (
    <div role="status" data-testid="refusal-notice" className="fixed inset-x-3 bottom-3 z-[55] mx-auto flex max-w-xl items-start gap-3 rounded-lg border border-border bg-surface-raised p-3 text-label text-foreground shadow-overlay">
      <p className="min-w-0 flex-1 break-words">{message}</p>
      <Button type="button" size="sm" variant="ghost" onClick={() => setMessage(null)}>
        Dismiss
      </Button>
    </div>
  );
}
