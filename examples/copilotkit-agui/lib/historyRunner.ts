// Restores a thread's history after a reload (or a restart of this server) from PydanticOS.
//
// CopilotKit's chat reattaches by asking the runtime's runner to `connect` to the thread. The
// default InMemoryAgentRunner replays only the events this process saw and holds in memory, so
// a thread it does not hold (another server instance, a restart, an evicted thread) comes back
// empty. This runner keeps that behaviour and, when the in-memory runner has nothing for the
// thread, loads PydanticOS's thread snapshot (GET /api/v1/ag-ui/{agents|workflows}/{id}/threads/
// {threadId}, with the same API key) and replays it as RUN_STARTED, MESSAGES_SNAPSHOT and
// RUN_FINISHED. Server-only: the key never leaves this process.
//
// Thread owners. CopilotKit scopes threads by `ownerId` (the anonymous owner "", or the user
// `identifyUser` returns). The API key is the tenant's, so PydanticOS cannot tell those owners
// apart: the fallback must. It never loads a thread this process holds for another owner, and
// for an identified caller it loads only when `canRead` confirms that the caller owns the thread
// (without `canRead`, identified callers get no fallback). An anonymous caller is served as
// CopilotKit serves anonymous threads: the thread id is the capability.
import { EventType, type BaseEvent, type Message } from "@ag-ui/client";
import { type AgentRunnerConnectRequest, InMemoryAgentRunner, ɵresolveThreadOwner } from "@copilotkit/runtime/v2";
import { Observable } from "rxjs";

import { type Target, endpointUrl } from "@/lib/agents";

export type HistoryLoader = (target: Target, threadId: string) => Promise<Message[]>;

/** True when `ownerId` (an identified CopilotKit user) owns `threadId`, from your own records. */
export type ThreadOwnerCheck = (ownerId: string, target: Target, threadId: string) => boolean | Promise<boolean>;

/** CopilotKit's owner of threads created without identity (no `identifyUser`). */
const ANONYMOUS_OWNER = "";

/** The thread's messages from PydanticOS, or [] when it has none (404) or cannot be read. */
export function pydanticosHistory(apiKey: string): HistoryLoader {
  return async (target, threadId) => {
    const response = await fetch(`${endpointUrl(target)}/threads/${encodeURIComponent(threadId)}`, {
      headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) return [];
    const body = (await response.json()) as { messages?: Message[] };
    return Array.isArray(body.messages) ? body.messages : [];
  };
}

export class PydanticOSHistoryRunner extends InMemoryAgentRunner {
  constructor(
    private readonly targets: ReadonlyMap<string, Target>,
    private readonly loadHistory: HistoryLoader,
    private readonly canRead?: ThreadOwnerCheck,
  ) {
    super();
  }

  override connect(request: AgentRunnerConnectRequest): Observable<BaseEvent> {
    const inMemory = super.connect(request);
    const target = request.agentId ? this.targets.get(request.agentId) : undefined;
    if (!target) return inMemory;
    return new Observable<BaseEvent>((subscriber) => {
      let replayed = false;
      let closed = false;
      const inner = inMemory.subscribe({
        next: (event) => {
          replayed = true;
          subscriber.next(event);
        },
        error: (error) => subscriber.error(error),
        complete: () => {
          if (replayed) return subscriber.complete();
          this.fallback(request, target)
            .then((messages) => {
              if (closed) return;
              if (messages.length > 0) {
                const runId = `pydanticos-history-${request.threadId}`;
                subscriber.next({ type: EventType.RUN_STARTED, threadId: request.threadId, runId } as BaseEvent);
                subscriber.next({ type: EventType.MESSAGES_SNAPSHOT, messages } as BaseEvent);
                subscriber.next({ type: EventType.RUN_FINISHED, threadId: request.threadId, runId } as BaseEvent);
              }
              subscriber.complete();
            })
            // History is a convenience: an unreadable thread connects empty, as before.
            .catch(() => {
              if (!closed) subscriber.complete();
            });
        },
      });
      return () => {
        closed = true;
        inner.unsubscribe();
      };
    });
  }

  /** The thread's PydanticOS history, or [] when this caller may not read it here. */
  private async fallback(request: AgentRunnerConnectRequest, target: Target): Promise<Message[]> {
    const owner = ɵresolveThreadOwner(request.ownerId);
    // Held in this process by another owner: CopilotKit already answered "unknown"; so do we.
    if (!this.isThreadAccessible(request.threadId, { ownerId: owner })) return [];
    if (owner !== ANONYMOUS_OWNER) {
      // An identified user (or the server owner): only with proof that they own the thread.
      if (!this.canRead || !(await this.canRead(owner, target, request.threadId))) return [];
    }
    return this.loadHistory(target, request.threadId);
  }
}
