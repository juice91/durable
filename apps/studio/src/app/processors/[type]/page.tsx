"use client";

import React from "react";
import { useParams } from "next/navigation";
import { ProcessorDetail } from "@/components/processors/ProcessorDetail";

/** The route segment as a type id; decoded once, kept raw if it is not valid percent-encoding. */
function typeParam(value: string | string[] | undefined): string {
  if (typeof value !== "string") return "";
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** `/processors/[type]`: one processor type (see ProcessorDetail). */
export default function ProcessorTypePage() {
  const params = useParams();
  const type = typeParam(params.type);
  return <ProcessorDetail type={type} />;
}
