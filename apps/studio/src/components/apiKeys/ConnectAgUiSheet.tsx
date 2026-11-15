"use client";

import React from "react";
import Link from "next/link";
import { Plug } from "lucide-react";
import {
  API_KEY_ENV_VAR,
  aguiAttachUrl,
  aguiEndpoint,
  aguiThreadUrl,
  copilotKitSnippet,
  curlSnippet,
  type ConnectTarget,
} from "@/lib/apiKeys";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { routes } from "@/lib/routes";
import { IconButton } from "@/components/ui/IconButton";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { useUrlSheet } from "@/components/ui/useUrlSheet";
import { CopyField } from "./CopyField";

/** The query parameter that holds the open Connect Sheet (`?connect=ag-ui`). */
export const CONNECT_PARAM = "connect";
export const CONNECT_VALUE = "ag-ui";
export const CONNECT_TITLE = "Connect (AG-UI)";

function Heading({ children }: { children: React.ReactNode }) {
  return <h3 className="text-label font-semibold text-foreground">{children}</h3>;
}

/** The Sheet's body: endpoints and snippets built from the URL and id only, never a key. */
export function ConnectAgUiBody({ target, name }: { target: ConnectTarget; name: string | null }) {
  const noun = target.kind === "agent" ? "agent" : "workflow";
  return (
    <div className="flex min-w-0 flex-col gap-5" data-testid="connect-agui-body">
      <p className="text-label text-muted-foreground">
        Any AG-UI client (CopilotKit, <code className="font-mono">@ag-ui/client</code>, assistant-ui) can run {name ? <strong className="font-medium text-foreground">{name}</strong> : `this ${noun}`}. Calls
        run its latest published version.
      </p>

      <section className="flex min-w-0 flex-col gap-3" aria-label="Endpoints">
        <Heading>Endpoints</Heading>
        <CopyField label="Run (POST)" value={aguiEndpoint(target)} testId="connect-agui-endpoint" copiedMessage="Endpoint copied" />
        <CopyField label="Thread messages (GET)" value={aguiThreadUrl(target)} testId="connect-agui-thread" copiedMessage="URL copied" />
        <CopyField
          label="Attach or resume a run (GET)"
          value={aguiAttachUrl(target)}
          testId="connect-agui-attach"
          copiedMessage="URL copied"
          hint="Send Last-Event-ID to resume after a dropped connection."
        />
      </section>

      <section className="flex min-w-0 flex-col gap-2" aria-label="Authentication">
        <Heading>Authentication</Heading>
        <p className="text-label text-muted-foreground">
          Send an API key as <code className="font-mono text-foreground">Authorization: Bearer &lt;key&gt;</code>. The snippets read it from{" "}
          <code className="font-mono text-foreground">{API_KEY_ENV_VAR}</code>. A key is shown only once, when it is created; it is never shown here.
        </p>
        <p className="text-label">
          <Link href={routes.settingsApiKeys()} className="text-foreground underline underline-offset-4 hover:no-underline" data-testid="connect-agui-keys-link">
            Manage API keys in Settings
          </Link>
        </p>
      </section>

      <section className="flex min-w-0 flex-col gap-3" aria-label="Snippets">
        <Heading>CopilotKit</Heading>
        <CopyField label="Runtime route (Next.js)" value={copilotKitSnippet(target)} multiline testId="connect-agui-copilotkit" copiedMessage="Snippet copied" />
        <Heading>curl</Heading>
        <CopyField
          label="One turn"
          value={curlSnippet(target)}
          multiline
          testId="connect-agui-curl"
          copiedMessage="Snippet copied"
          hint={
            target.kind === "workflow"
              ? "The workflow's input goes in forwardedProps.pydanticos.input. A thread is one workflow run: start each new run under a new threadId; later runs of a thread only answer its approvals (resume)."
              : undefined
          }
        />
      </section>

      <p className="text-caption text-muted-foreground">
        Keep the key on a server (the CopilotKit runtime, your backend). A browser calling the API directly needs its origin in the deployment&apos;s AG-UI CORS
        list and in the key&apos;s allowed origins.
      </p>
    </div>
  );
}

/**
 * The header action and its URL-backed Sheet (`?connect=ag-ui`), for the agent page and the
 * workflow pages. Needs a `<Suspense>` boundary above it (useSearchParams).
 */
export function ConnectAgUiAction({ target, name }: { target: ConnectTarget; name: string | null }) {
  const sheet = useUrlSheet(CONNECT_PARAM, { history: "push" });
  const open = sheet.value === CONNECT_VALUE;
  return (
    <>
      {/* Its own provider: workflow headers can render outside the app shell (tests, embeds). */}
      <TooltipProvider>
        <IconButton
          label={CONNECT_TITLE}
          size="sm"
          aria-haspopup="dialog"
          aria-expanded={open}
          onClick={() => sheet.open(CONNECT_VALUE)}
          icon={<Plug strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
          data-testid="connect-agui-open"
        />
      </TooltipProvider>
      <Sheet open={open} onOpenChange={sheet.onOpenChange}>
        <SheetContent size="md" data-testid="connect-agui-sheet">
          <SheetHeader title={CONNECT_TITLE} description={name ?? (target.kind === "agent" ? "This agent" : "This workflow")} />
          <SheetBody>
            <ConnectAgUiBody target={target} name={name} />
          </SheetBody>
        </SheetContent>
      </Sheet>
    </>
  );
}
