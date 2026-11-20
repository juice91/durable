"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { approvalSubject, onPendingChange, type Approval } from "@/lib/approvals";
import { routes } from "@/lib/routes";
import { toast } from "@/components/ui/Toaster";

const SUMMARY_CHARS = 60;

/** The card's words for the toast: its summary, else its prompt, else what it gates; cut at 60 characters. */
export function newCardText(approval: Approval): string {
  const text = (approval.summary || approval.prompt || approvalSubject(approval)).replace(/\s+/g, " ").trim();
  return Array.from(text).slice(0, SUMMARY_CHARS).join("");
}

/**
 * Announces each new pending approval card once, on any Studio page: a toast with 「查看」 that opens
 * the card in the inbox, and the same words in a polite live region for screen readers. Reads the
 * shared pending poll (the sidebar badge's), so it adds no request of its own.
 */
export function PendingApprovalToasts() {
  const router = useRouter();
  const [announcement, setAnnouncement] = useState("");

  useEffect(
    () =>
      onPendingChange(({ added }) => {
        for (const approval of added) {
          const message = `新的審核卡：${newCardText(approval)}`;
          setAnnouncement(message);
          toast(message, {
            action: { label: "查看", onClick: () => router.push(routes.approval(approval.run_id, approval.approval_key)) },
          });
        }
      }),
    [router],
  );

  return (
    <div role="status" data-testid="pending-approval-announcement" className="sr-only">
      {announcement}
    </div>
  );
}
