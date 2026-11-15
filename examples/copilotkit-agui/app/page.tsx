// Server component: reads which agents the runtime serves (names and kinds only; the key and the
// API URL stay on the server) and renders the chat for the one picked with `?agent=<name>`, on
// the thread `?thread=<id>` (a new one when absent; the URL keeps it, so a reload stays on it).
import { configuredTargets } from "@/lib/agents";

import { Chat } from "./chat";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }: { searchParams: Promise<{ agent?: string; thread?: string }> }) {
  const targets = configuredTargets().map(({ name, kind }) => ({ name, kind }));
  if (targets.length === 0) {
    return (
      <main className="empty">
        <h1>Nothing to connect to</h1>
        <p>
          Set <code>PYDANTICOS_API_KEY</code> and <code>PYDANTICOS_AGENT_ID</code> in <code>.env.local</code> (see{" "}
          <code>.env.example</code>), then restart <code>pnpm dev</code>.
        </p>
      </main>
    );
  }
  const { agent, thread } = await searchParams;
  const current = targets.find((target) => target.name === agent) ?? targets[0];
  return <Chat targets={targets} current={current} initialThreadId={thread} />;
}
