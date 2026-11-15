"use client";

import React, { Suspense } from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import { ApprovalsInbox } from "@/components/approvals/ApprovalsInbox";
import { navCrumb } from "@/lib/nav";

export default function ApprovalsPage() {
  // useSearchParams (filters and the approval Sheet) needs a Suspense boundary for the static prerender.
  return (
    <>
      <PageHeader crumbs={[navCrumb("approvals", { current: true })]} />
      <Suspense fallback={<p className="py-12 text-center text-muted-foreground">Loading approvals...</p>}>
        <ApprovalsInbox />
      </Suspense>
    </>
  );
}
