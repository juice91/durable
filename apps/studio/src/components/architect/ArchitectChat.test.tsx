import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/architect",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/agentPage/ChatTab", () => ({
  ChatTab: ({ agentId, syncUrl }: { agentId: string; syncUrl?: boolean }) => (
    <div data-testid="chat-tab" data-agent={agentId} data-sync-url={String(syncUrl)} />
  ),
}));

import { ArchitectChat } from "./ArchitectChat";
import { ApprovalCard } from "@/components/approvals/ApprovalCard";
import * as InboxHost from "@/components/approvals/InboxApprovalCard";

const SUMMARY = "Publish faq-bot as version 3. Warnings: it can send email.";
const call = { kind: "tool" as const, runId: "run-1", toolCallId: "call-1", toolName: "architect.publish_agent", args: { agent_id: "faq-bot" }, deadline: null, summary: SUMMARY };

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200, headers: { "content-type": "application/json" } })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("ArchitectChat", () => {
  it("mounts the Architect agent's conversation without writing the address bar", () => {
    render(<ArchitectChat architectAgentId="row-arch" onChanged={vi.fn()} />);
    const chat = screen.getByTestId("chat-tab");
    expect(chat).toHaveAttribute("data-agent", "row-arch");
    expect(chat).toHaveAttribute("data-sync-url", "false");
  });

  it("says so while the Architect is not set up", () => {
    render(<ArchitectChat architectAgentId={null} onChanged={vi.fn()} />);
    expect(screen.queryByTestId("chat-tab")).toBeNull();
    expect(screen.getByTestId("architect-chat-unavailable")).toBeInTheDocument();
  });

  it("an approval card for architect.publish_agent shows the summary with Approve and Deny", () => {
    render(<ApprovalCard approval={call} onRefresh={vi.fn()} />);
    expect(screen.getByTestId("approval-summary-tool-call-1")).toHaveTextContent(SUMMARY);
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });

  it("calls onChanged after an approval decision so the created list refreshes", async () => {
    const onChanged = vi.fn();
    const fetchMock = vi.fn(async (url: string) =>
      String(url).includes("/tool-approvals/")
        ? new Response(JSON.stringify({ run_id: "run-1", tool_call_id: "call-1", decision: "approved", decided_by: "dev", comment: null, notified: true }), { status: 200, headers: { "content-type": "application/json" } })
        : new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <>
        <ArchitectChat architectAgentId="row-arch" onChanged={onChanged} />
        <ApprovalCard approval={call} onRefresh={vi.fn()} />
      </>,
    );
    expect(onChanged).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });
});

describe("InboxApprovalCard", () => {
  const item = (over: Record<string, unknown>) => ({
    run_id: "run-1", kind: "tool", approval_key: "call-1", tool_call_id: "call-1", step_id: null, root_run_id: "run-1", agent_id: "a", workflow_id: null,
    tool_name: "architect.publish_agent", args: { agent_id: "faq-bot" }, prompt: null, status: "pending", requested_at: null, deadline: null,
    decided_at: null, decision: null, decided_by: null, run_status: "running", summary: SUMMARY, ...over,
  });

  it("shows the summary and links architect.* tools that name a slug to /architect", () => {
    render(<InboxHost.InboxApprovalCard approval={item({}) as never} onConflict={vi.fn()} onDecided={vi.fn()} />);
    expect(screen.getByTestId("approval-summary-tool-call-1")).toHaveTextContent(SUMMARY);
    expect(screen.getByRole("link", { name: /Open in Architect/ })).toHaveAttribute("href", "/architect");
  });

  it("has no Architect link for other tools", () => {
    render(<InboxHost.InboxApprovalCard approval={item({ tool_name: "send_email", args: { to: "x" }, summary: null }) as never} onConflict={vi.fn()} onDecided={vi.fn()} />);
    expect(screen.queryByRole("link", { name: /Open in Architect/ })).toBeNull();
  });
});
