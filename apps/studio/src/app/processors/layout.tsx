import React from "react";
import { ProcessorsDataProvider } from "@/components/processors/processorData";

/** Shares the catalog and "Used by" data between `/processors` and `/processors/[type]`. */
export default function ProcessorsLayout({ children }: { children: React.ReactNode }) {
  return <ProcessorsDataProvider>{children}</ProcessorsDataProvider>;
}
