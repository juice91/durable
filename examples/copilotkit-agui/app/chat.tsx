"use client";

// The browser side: CopilotKit's chat against our runtime route, plus
// - a frontend tool (useFrontendTool) the agent can call: it runs here, in the browser;
// - a human-in-the-loop tool (useHumanInTheLoop): the agent asks, the user answers here;
// - approvals (useInterrupt): a gated server tool or a workflow approval step ends the AG-UI run
//   with an interrupt; answering it resumes the same durable run on the server;
// - server tool calls (MCP and built-in) shown with CopilotKit's default tool card;
// - a workflow's read-only state (STATE_SNAPSHOT / STATE_DELTA) in the side panel.
//
// Neither client tool ever runs on the server: PydanticOS ends the turn with the call pending, and
// CopilotKit sends the result in the next AG-UI run, which continues the turn.
import {
  CopilotChat,
  CopilotKitProvider,
  UseAgentUpdate,
  useAgent,
  useDefaultRenderTool,
  useFrontendTool,
  useHumanInTheLoop,
  useInterrupt,
} from "@copilotkit/react-core/v2";
import { useEffect, useState } from "react";
import { z } from "zod";

type TargetView = { name: string; kind: "agent" | "workflow" };

export function Chat({
  targets,
  current,
  initialThreadId,
}: {
  targets: TargetView[];
  current: TargetView;
  initialThreadId?: string;
}) {
  // The thread lives in the URL (?thread=), so a reload stays on it; "New thread" starts another.
  // A workflow thread is one workflow run: start a new thread for each run.
  const [threadId, setThreadId] = useState(() => initialThreadId || crypto.randomUUID());
  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get("thread") === threadId) return;
    url.searchParams.set("agent", current.name);
    url.searchParams.set("thread", threadId);
    window.history.replaceState(null, "", url);
  }, [current.name, threadId]);
  return (
    <CopilotKitProvider runtimeUrl="/api/copilotkit" useSingleEndpoint agentId={current.name} showDevConsole={false} enableInspector={false}>
      <div className="shell">
        <header className="bar">
          <strong>PydanticOS + CopilotKit</strong>
          <nav aria-label="Agents">
            {targets.map((target) => (
              <a key={target.name} href={`?agent=${target.name}`} aria-current={target.name === current.name ? "page" : undefined}>
                {target.name}
                {target.kind === "workflow" ? " (workflow)" : ""}
              </a>
            ))}
          </nav>
          <button type="button" onClick={() => setThreadId(crypto.randomUUID())}>
            New thread
          </button>
        </header>
        {current.kind === "agent" ? <ClientTools agentId={current.name} /> : null}
        <Approvals agentId={current.name} />
        <ServerToolCards />
        <div className="main">
          <section className="chat" aria-label="Chat">
            <CopilotChat key={`${current.name}:${threadId}`} agentId={current.name} threadId={threadId} />
          </section>
          {current.kind === "workflow" ? <WorkflowState agentId={current.name} /> : null}
        </div>
      </div>
    </CopilotKitProvider>
  );
}

function ClientTools({ agentId }: { agentId: string }) {
  // Scoped to this agent: CopilotKit sends every unscoped tool with every run, and a workflow run
  // refuses client tools (422 run_option_unsupported).

  // A frontend tool: the browser runs it and returns the result to the agent.
  const [accent, setAccent] = useState<string | null>(null);
  useFrontendTool(
    {
      name: "set_theme_color",
      description: "Set the page's accent colour. Use a CSS colour name or #rrggbb.",
      parameters: z.object({ color: z.string().describe("A CSS colour, e.g. teal or #0a7") }),
      agentId,
      handler: async ({ color }) => {
        document.documentElement.style.setProperty("--accent", color);
        setAccent(color);
        return `The accent colour is now ${color}.`;
      },
    },
    [agentId],
  );

  // A human-in-the-loop tool: the browser asks the user, and their answer is the tool's result.
  useHumanInTheLoop(
    {
      name: "confirm_with_user",
      description: "Ask the user a yes/no question and wait for the answer.",
      parameters: z.object({ question: z.string().describe("The question to ask") }),
      agentId,
      render: ({ args, status, respond, result }) => (
        <div className="card" data-testid="hitl-card">
          <p>{args.question ?? "…"}</p>
          {status === "executing" && respond ? (
            <div className="actions">
              <button type="button" onClick={() => void respond({ confirmed: true })}>
                Yes
              </button>
              <button type="button" onClick={() => void respond({ confirmed: false })}>
                No
              </button>
            </div>
          ) : null}
          {status === "complete" ? <p className="muted">Answered: {String(result)}</p> : null}
        </div>
      ),
    },
    [agentId],
  );

  return accent ? (
    <p className="status" role="status">
      Accent set by the agent: <code>{accent}</code>
    </p>
  ) : null;
}

function Approvals({ agentId }: { agentId: string }) {
  // Approvals: PydanticOS ends the run with RUN_FINISHED{outcome: interrupt}. Resolving sends
  // resume[{interruptId, status: "resolved", payload: {approved, reason?}}]; cancel() sends
  // status "cancelled" (a denial). The approved tool then runs once, on the server.
  useInterrupt({
    agentId,
    render: ({ interrupt, resolve, cancel }) => (
      <div className="card approval" data-testid="approval-card">
        <p>
          <strong>{interrupt?.reason === "tool_call" ? "Approve this tool call?" : "Approval needed"}</strong>
        </p>
        <p>{interrupt?.message ?? "The agent is waiting for a decision."}</p>
        {interrupt?.expiresAt ? <p className="muted">Expires {new Date(interrupt.expiresAt).toLocaleString()}</p> : null}
        <div className="actions">
          <button type="button" onClick={() => void resolve({ approved: true })}>
            Approve
          </button>
          <button type="button" onClick={() => void resolve({ approved: false, reason: "Denied in the CopilotKit example" })}>
            Deny
          </button>
          <button type="button" onClick={() => void cancel()}>
            Cancel
          </button>
        </div>
      </div>
    ),
  });
  return null;
}

function ServerToolCards() {
  // Server tool calls (MCP, built-in) render with CopilotKit's default card: name, args, result.
  useDefaultRenderTool();
  return null;
}

function WorkflowState({ agentId }: { agentId: string }) {
  const { agent } = useAgent({ agentId, updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged] });
  return (
    <aside className="state" aria-label="Workflow state">
      <h2>Workflow state</h2>
      <p className="muted">
        Read-only, from <code>STATE_SNAPSHOT</code> and <code>STATE_DELTA</code>. Send the workflow input as JSON, e.g.{" "}
        <code>{'{"q": "refund order 42"}'}</code>.
      </p>
      <pre data-testid="workflow-state">{JSON.stringify(agent.state ?? {}, null, 2)}</pre>
    </aside>
  );
}
