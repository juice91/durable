"use client";

import { useCan } from "@/lib/permissions";
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, type Agent, type AgentVersion } from "@/lib/api";
import { cn } from "@/lib/cn";
import { listConversations } from "@/lib/conversations";
import { listAgentWorkingMemories, resourceIdProblem, RESOURCE_ID_MAX_LENGTH, type ForgetResult } from "@/lib/memory";
import { routes } from "@/lib/routes";
import { Button, focusRing } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toaster";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";
import { useAgentPage } from "@/components/agentPage/AgentPageContext";
import { WorkingMemoryGuard } from "./WorkingMemoryGuard";
import { WorkingMemoryEditor } from "./WorkingMemoryEditor";
import { ForgetUserDialog, forgetSummary } from "./ForgetUserDialog";
import {
  WORKING_MEMORY_GUARD_COPY,
  agentHasMemory,
  editBlockedReason,
  latestPublishedSpec,
  workingFormat,
  workingOf,
  workingScope,
  workingStarter,
} from "./memoryModel";

/** How many users the list offers (working memory notes and recent conversations, merged). */
const KNOWN_USERS_LIMIT = 100;

interface KnownUser {
  id: string;
  /** "Working memory" and/or "Conversations". */
  sources: string[];
}

/** The agent and its versions: from the agent page, or loaded here when the tab renders alone. */
function useMemoryAgent(agentId: string): { agent: Agent | null; versions: AgentVersion[] | null; error: string | null } {
  const page = useAgentPage();
  const shared = page !== null && page.agentId === agentId;
  const [agent, setAgent] = useState<Agent | null>(null);
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (shared) return;
    let cancelled = false;
    Promise.all([api.agents.get(agentId), api.versions.list(agentId).catch(() => [] as AgentVersion[])])
      .then(([loaded, items]) => {
        if (cancelled) return;
        setAgent(loaded);
        setVersions(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load the agent.");
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, shared]);
  if (shared) return { agent: page.agent, versions: page.versions, error: page.loadError };
  return { agent, versions, error };
}

/**
 * The agent page's Memory tab (`/agents/:id/memory?resource=`, RM.18): pick a user (resource id)
 * from those with working memory or conversations, or enter one; view, edit and clear their
 * per-user working memory; Forget user. An agent without memory has no Memory tab: this page sends
 * it to Chat.
 */
export function MemoryTab({ agentId }: { agentId: string }) {
  const router = useRouter();
  const search = useSearchParams();
  const mayEdit = useCan("agent", agentId, "edit");
  const resource = search?.get("resource") || null;
  const { agent, versions, error: agentError } = useMemoryAgent(agentId);
  const [users, setUsers] = useState<KnownUser[] | null>(null);
  const [usersError, setUsersError] = useState<string | null>(null);
  const [usersKey, setUsersKey] = useState(0);
  const [entry, setEntry] = useState("");
  const [entryError, setEntryError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [pendingResource, setPendingResource] = useState<string | null | undefined>(undefined);
  const [forgetting, setForgetting] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const inputId = useId();
  const errorId = useId();

  const hasMemory = agentHasMemory(agent, versions);
  const ready = agent !== null && versions !== null;

  // No memory (draft and latest version): there is no Memory tab, so go to Chat.
  useEffect(() => {
    if (ready && !hasMemory) router.replace(routes.agentChat(agentId));
  }, [agentId, hasMemory, ready, router]);

  // The users to pick from: per-user working memory notes, then recent conversations' users.
  useEffect(() => {
    if (!ready || !hasMemory) return;
    let cancelled = false;
    setUsersError(null);
    Promise.allSettled([listAgentWorkingMemories(agentId, "resource", KNOWN_USERS_LIMIT), listConversations({ agentId, limit: 50 })]).then(([notes, conversations]) => {
      if (cancelled) return;
      const known = new Map<string, Set<string>>();
      const add = (id: string | null | undefined, source: string) => {
        if (!id) return;
        if (!known.has(id)) known.set(id, new Set());
        known.get(id)!.add(source);
      };
      if (notes.status === "fulfilled") notes.value.forEach((note) => add(note.scope_key, "Working memory"));
      if (conversations.status === "fulfilled") conversations.value.items.forEach((item) => add(item.resource_id, "Conversations"));
      setUsers([...known].slice(0, KNOWN_USERS_LIMIT).map(([id, sources]) => ({ id, sources: [...sources] })));
      if (notes.status === "rejected" && conversations.status === "rejected") setUsersError("Failed to load the users.");
    });
    return () => {
      cancelled = true;
    };
  }, [agentId, hasMemory, ready, usersKey]);

  const go = useCallback(
    (next: string | null) => {
      setEntryError(null);
      router.push(routes.agentMemory(agentId, { resource: next }));
    },
    [agentId, router],
  );

  /** Another user (or none): asks first while a working memory edit is unsaved. */
  const choose = (next: string | null) => {
    if (next === resource) return;
    if (dirty) setPendingResource(next);
    else go(next);
  };

  const open = (event: React.FormEvent) => {
    event.preventDefault();
    const value = entry.trim();
    if (!value) {
      setEntryError("Enter a user id.");
      return;
    }
    const problem = resourceIdProblem(value);
    if (problem) {
      setEntryError(problem);
      return;
    }
    setEntry("");
    choose(value);
  };

  // `c`: enter a user id.
  useCreateAction(hasMemory ? () => inputRef.current?.focus() : null);

  const forgotten = (result: ForgetResult) => {
    setForgetting(false);
    toast.success(forgetSummary(result));
    if (result.busy_conversations.length > 0) {
      toast.error(`${result.busy_conversations.length} conversation(s) had a turn running and were kept; forget again when it ends.`);
    }
    setUsersKey((n) => n + 1);
    setDirty(false);
    router.replace(routes.agentMemory(agentId));
    // The Forget button goes with the user: focus the user id field, never <body>. After the
    // dialog's own focus return (its unmount), hence the next task.
    window.setTimeout(() => inputRef.current?.focus(), 0);
  };

  if (agentError && !agent) {
    return (
      <p role="alert" className="p-4 text-destructive-text">
        {agentError}
      </p>
    );
  }
  if (!ready) {
    return (
      <p role="status" className="p-4 text-label text-muted-foreground">
        Loading memory…
      </p>
    );
  }
  if (!hasMemory) {
    return (
      <p role="status" data-testid="memory-tab-no-memory" className="p-4 text-label text-muted-foreground">
        This agent has no memory. Opening Chat…
      </p>
    );
  }

  const published = latestPublishedSpec(agent, versions);
  const working = workingOf(published) ?? workingOf(agent.draft_spec);
  const perConversation = working !== null && workingScope(working) === "conversation";
  const invalidResource = resource !== null && resourceIdProblem(resource) !== null;

  return (
    <div data-testid="memory-tab" className="mx-auto flex w-full min-w-0 max-w-5xl flex-col gap-6 p-4 lg:flex-row">
      <section aria-labelledby="memory-users-title" className="min-w-0 space-y-3 lg:w-72 lg:shrink-0">
        <h2 id="memory-users-title" className="text-title font-semibold text-foreground">
          Users
        </h2>
        <p className="text-caption text-muted-foreground">A user is the resource id a conversation acts for (your user id unless “Act as user” chose another).</p>
        <form onSubmit={open} className="min-w-0 space-y-1" data-testid="memory-resource-form">
          <label htmlFor={inputId} className="text-label font-medium text-foreground">
            User id
          </label>
          <div className="flex min-w-0 gap-2">
            <input
              ref={inputRef}
              id={inputId}
              data-testid="memory-resource-input"
              value={entry}
              maxLength={RESOURCE_ID_MAX_LENGTH}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => {
                setEntry(event.target.value);
                setEntryError(null);
              }}
              aria-invalid={Boolean(entryError)}
              aria-describedby={entryError ? errorId : undefined}
              className={cn("h-8 min-w-0 flex-1 rounded-md border border-input bg-surface px-2 font-mono text-label text-foreground", focusRing)}
            />
            <Button type="submit" data-testid="memory-resource-open">
              Open
            </Button>
          </div>
          {entryError ? (
            <p id={errorId} role="alert" data-testid="memory-resource-error" className="text-caption text-destructive-text">
              {entryError}
            </p>
          ) : null}
        </form>
        {usersError ? (
          <p role="alert" className="text-label text-destructive-text">
            {usersError}
          </p>
        ) : users === null ? (
          <p role="status" className="text-label text-muted-foreground">
            Loading users…
          </p>
        ) : users.length === 0 ? (
          <p data-testid="memory-resource-empty" className="text-label text-muted-foreground">
            No users with memory or conversations yet.
          </p>
        ) : (
          <ul data-testid="memory-resource-list" aria-label="Users" className="min-w-0 space-y-0.5">
            {users.map((user) => (
              <li key={user.id}>
                <button
                  type="button"
                  data-testid="memory-resource-option"
                  aria-current={user.id === resource ? "true" : undefined}
                  onClick={() => choose(user.id)}
                  className={cn(
                    "w-full min-w-0 rounded-md px-2 py-1.5 text-left hover:bg-fill-hover",
                    user.id === resource && "bg-fill-selected",
                    focusRing,
                  )}
                >
                  <span className="block truncate font-mono text-label text-foreground">{user.id}</span>
                  <span className="block truncate text-caption text-muted-foreground">{user.sources.join(" · ")}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="memory-user-title" className="min-w-0 flex-1 space-y-4" data-testid="memory-user">
        {resource === null ? (
          <>
            <h2 id="memory-user-title" className="text-title font-semibold text-foreground">
              Memory per user
            </h2>
            <p className="text-label text-muted-foreground">Pick a user, or enter a user id, to see and edit their working memory or forget them.</p>
          </>
        ) : invalidResource ? (
          <>
            <h2 id="memory-user-title" className="text-title font-semibold text-foreground">
              Not a user id
            </h2>
            <p role="alert" className="text-label text-destructive-text">
              {resourceIdProblem(resource)}
            </p>
          </>
        ) : (
          <>
            <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
              <h2 id="memory-user-title" className="min-w-0 truncate font-mono text-title font-semibold text-foreground" data-testid="memory-user-title">
                {resource}
              </h2>
              {mayEdit ? (
                <Button variant="destructive" onClick={() => setForgetting(true)} data-testid="memory-forget-user">
                  Forget user
                </Button>
              ) : null}
            </div>
            {perConversation ? (
              <p className="text-caption text-muted-foreground" data-testid="memory-per-conversation">
                This agent keeps working memory per conversation: each conversation&apos;s notes are in its memory card in Chat. A per-user note left from
                earlier settings can still be viewed and cleared here.
              </p>
            ) : null}
            {working ? (
              <WorkingMemoryEditor
                key={resource}
                mayChange={mayEdit}
                target={{ scope: "resource", agentId, resourceId: resource }}
                format={workingFormat(working)}
                starter={workingStarter(working)}
                editBlocked={editBlockedReason(published, "resource")}
                onDirtyChange={setDirty}
                headingLevel={2}
              />
            ) : (
              <p className="text-label text-muted-foreground">This agent has no working memory. Forget user still deletes what recall indexed for them.</p>
            )}
          </>
        )}
      </section>

      {forgetting && resource ? (
        <ForgetUserDialog agentId={agentId} agentName={agent.name} resourceId={resource} onDone={forgotten} onCancel={() => setForgetting(false)} />
      ) : null}
      {dirty ? <WorkingMemoryGuard /> : null}
      {pendingResource !== undefined ? (
        <ConfirmDialog
          title={WORKING_MEMORY_GUARD_COPY.title}
          description="The working memory edit is not saved. Opening another user discards it."
          confirmLabel={WORKING_MEMORY_GUARD_COPY.confirmLabel}
          cancelLabel="Stay"
          destructive
          onConfirm={() => {
            const next = pendingResource;
            setPendingResource(undefined);
            setDirty(false);
            go(next ?? null);
          }}
          onCancel={() => setPendingResource(undefined)}
        />
      ) : null}
    </div>
  );
}
