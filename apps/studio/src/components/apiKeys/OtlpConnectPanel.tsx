"use client";

import React from "react";
import { otlpEnvSnippet, otlpJavaScriptSnippet, otlpPythonSnippet, otlpTracesEndpoint } from "@/lib/apiKeys";
import { CopyField } from "./CopyField";

/**
 * "Send OpenTelemetry traces": the OTLP endpoint and the connect snippets for the OpenTelemetry SDK
 * (Python and JavaScript). The snippets name the key by an environment variable, never by value, so
 * nothing here is secret. A key needs the `otlp` scope.
 */
export function OtlpConnectPanel() {
  return (
    <section aria-labelledby="settings-otlp-connect" data-testid="settings-otlp-connect" className="flex min-w-0 flex-col gap-3 border-t border-border pt-3">
      <div className="flex min-w-0 flex-col gap-1">
        <h3 id="settings-otlp-connect" className="text-label font-semibold text-foreground">
          Send OpenTelemetry traces
        </h3>
        <p className="text-label text-muted-foreground">
          A program outside the platform can send OpenTelemetry traces (OTLP over HTTP, protobuf or JSON) with a key created with{" "}
          <span className="font-medium text-foreground">Send traces (OTLP)</span>; they show up in the Traces page as external traces. Set{" "}
          <span className="font-mono">pydanticos.run_id</span> on a span to link it to a platform run.
        </p>
      </div>
      <CopyField label="OTLP traces endpoint" value={otlpTracesEndpoint()} testId="otlp-endpoint" copiedMessage="Endpoint copied" />
      <CopyField label="Environment" value={otlpEnvSnippet()} multiline testId="otlp-snippet-env" copiedMessage="Snippet copied" />
      <CopyField label="Python" value={otlpPythonSnippet()} multiline testId="otlp-snippet-python" copiedMessage="Snippet copied" />
      <CopyField label="JavaScript" value={otlpJavaScriptSnippet()} multiline testId="otlp-snippet-javascript" copiedMessage="Snippet copied" />
    </section>
  );
}
