"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { MoreHorizontal } from "lucide-react";
import { PageHeader } from "@/components/frame/PageHeader";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { LoadMore } from "@/components/evals/overview/LoadMore";
import { usePagedList } from "@/components/evals/overview/usePagedList";
import { fieldClass, labelClass } from "@/components/evals/evalsUi";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/DropdownMenu";
import { toast } from "@/components/ui/Toaster";
import { api } from "@/lib/api";
import {
  cloneConversation,
  deleteConversation,
  listConversations,
  renameConversation,
  type Conversation,
} from "@/lib/conversations";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { Select } from "@/components/ui/Select";

const TITLE_MAX_LENGTH = 200;

const when = (iso: string) => <span className="whitespace-nowrap">{new Date(iso).toLocaleString()}</span>;
const labelOf = (c: Conversation) => c.title?.trim() || "Untitled conversation";

interface Actions {
  onRename: (c: Conversation) => void;
  onClone: (c: Conversation) => void;
  onDelete: (c: Conversation) => void;
}

function columnsFor(names: Record<string, string>, actions: Actions): DataListColumn<Conversation>[] {
  return [
    {
      id: "title",
      header: "Title",
      sortable: true,
      sortValue: (c) => labelOf(c),
      cell: (c) => <span className="truncate">{labelOf(c)}</span>,
    },
    {
      id: "agent",
      header: "Agent",
      sortable: true,
      sortValue: (c) => names[c.agent_id] ?? "",
      cell: (c) => <span className="truncate">{names[c.agent_id] ?? `${c.agent_id.slice(0, 8)}…`}</span>,
    },
    {
      id: "user",
      header: "User",
      className: "hidden md:table-cell",
      cell: (c) => (c.resource_id ? <span className="truncate font-mono text-caption">{c.resource_id}</span> : <span className="text-muted-foreground">-</span>),
    },
    { id: "turns", header: "Turns", align: "end", sortable: true, sortValue: (c) => c.turn_count, cell: (c) => c.turn_count, className: "tabular-nums" },
    { id: "updated", header: "Last activity", sortable: true, sortValue: (c) => c.updated_at, cell: (c) => when(c.updated_at) },
    { id: "created", header: "Created", className: "hidden lg:table-cell", sortable: true, sortValue: (c) => c.created_at, cell: (c) => when(c.created_at) },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "end",
      cell: (c) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label={`Actions for ${labelOf(c)}`}
              data-testid="thread-actions"
              className="inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-fill-hover hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <MoreHorizontal aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-icon" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem data-testid="thread-rename" onSelect={() => actions.onRename(c)}>Rename</DropdownMenuItem>
            <DropdownMenuItem data-testid="thread-clone" onSelect={() => actions.onClone(c)}>Clone conversation</DropdownMenuItem>
            <DropdownMenuItem destructive data-testid="thread-delete" onSelect={() => actions.onDelete(c)}>Delete conversation</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ),
    },
  ];
}

/** The list for the URL's filters; a filter change reads the first page again (the search box keeps focus). */
function ThreadsList({
  agentId,
  q,
  names,
  agentOptions,
  setParam,
}: {
  agentId: string;
  q: string;
  names: Record<string, string>;
  agentOptions: Array<[string, string]>;
  setParam: (patch: { agent_id?: string; q?: string }) => void;
}) {
  const list = usePagedList(
    async (cursor) => {
      const page = await listConversations({ agentId: agentId || undefined, q: q || undefined, before: cursor, limit: 50 });
      return { items: page.items, next_cursor: page.next_before };
    },
    "Failed to load threads",
    [agentId, q],
  );
  const [renaming, setRenaming] = useState<Conversation | null>(null);
  const [draft, setDraft] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const startRename = (c: Conversation) => {
    setRenaming(c);
    setDraft(c.title ?? "");
    setRenameError(null);
  };
  const saveRename = async () => {
    if (!renaming) return;
    const title = draft.trim();
    if (title.length < 1 || title.length > TITLE_MAX_LENGTH) {
      setRenameError(`A title has 1 to ${TITLE_MAX_LENGTH} characters.`);
      return;
    }
    setBusy(true);
    try {
      await renameConversation(renaming.id, title);
      setRenaming(null);
      toast.success("Conversation renamed");
      list.reload();
    } catch (error) {
      setRenameError(error instanceof Error && error.message ? error.message : "Failed to rename the conversation.");
    } finally {
      setBusy(false);
    }
  };
  const clone = async (c: Conversation) => {
    try {
      await cloneConversation(c.id);
      toast.success("Conversation cloned");
      list.reload();
    } catch (error) {
      toast.error(error instanceof Error && error.message ? error.message : "Failed to clone the conversation.");
    }
  };
  const confirmDelete = async () => {
    if (!deleting) return;
    setBusy(true);
    setDeleteError(null);
    try {
      await deleteConversation(deleting.id);
      toast.success("Conversation deleted");
      setDeleting(null);
      list.reload();
    } catch (error) {
      // 404: already gone, which is what was asked.
      if (error instanceof Error && /not found|404/i.test(error.message)) {
        setDeleting(null);
        list.reload();
      } else {
        setDeleteError(error instanceof Error && error.message ? error.message : "Failed to delete the conversation.");
      }
    } finally {
      setBusy(false);
    }
  };

  const columns = useMemo(
    () => columnsFor(names, { onRename: startRename, onClone: (c) => void clone(c), onDelete: (c) => { setDeleteError(null); setDeleting(c); } }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [names],
  );

  return (
    <>
      <DataList<Conversation>
        label="Threads"
        testId="threads-list"
        rows={list.rows}
        columns={columns}
        getRowId={(c) => c.id}
        getRowHref={(c) => routes.agentChat(c.agent_id, { conversation: c.id })}
        getRowLabel={(c) => `Open ${labelOf(c)}, agent ${names[c.agent_id] ?? "unknown"}, ${c.turn_count} turns`}
        getRowTestId={() => "thread-row"}
        searchPlaceholder="Search by title"
        initialQuery={q}
        onSearchChange={(value) => {
          if (value.trim() !== q) setParam({ q: value.trim() });
        }}
        toolbar={
          <div className="flex min-w-0 items-center gap-2">
            <label htmlFor="threads-agent-filter" className={labelClass}>Agent</label>
            <Select
              id="threads-agent-filter"
              value={agentId}
              onValueChange={(v) => setParam({ agent_id: v })}
              className={`${fieldClass} max-w-48`}
            >
              <option value="">All agents</option>
              {agentOptions.map(([id, name]) => (
                <option key={id} value={id}>{name}</option>
              ))}
              {agentId && !names[agentId] ? <option value={agentId}>{agentId.slice(0, 8)}…</option> : null}
            </Select>
          </div>
        }
        noMatchText="No threads match the search."
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        empty={
          agentId || q
            ? { title: "No threads match", description: "Change or clear the filters to see more conversations." }
            : { title: "No threads yet", description: "Start a conversation in an agent's Chat tab. It appears here." }
        }
      />
      <LoadMore list={list} label="threads" />

      <Dialog open={renaming !== null} onOpenChange={(open) => { if (!open && !busy) setRenaming(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename conversation</DialogTitle>
            <DialogDescription>Renaming does not change the order of the list.</DialogDescription>
          </DialogHeader>
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              void saveRename();
            }}
          >
            <label htmlFor="thread-title" className={labelClass}>Title</label>
            <input id="thread-title" className={fieldClass} value={draft} maxLength={TITLE_MAX_LENGTH} onChange={(e) => setDraft(e.target.value)} />
            {renameError ? <p role="alert" className="text-label text-destructive-text">{renameError}</p> : null}
            <DialogFooter>
              <Button type="button" onClick={() => setRenaming(null)} disabled={busy}>Cancel</Button>
              <Button type="submit" variant="primary" disabled={busy}>Save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {deleting ? (
        <ConfirmDialog
          title="Delete this conversation?"
          description={`"${labelOf(deleting)}" and its memory are deleted. Its runs stay in Traces for cost and audit, with their text removed.`}
          confirmLabel="Delete conversation"
          destructive
          busy={busy}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onCancel={() => setDeleting(null)}
        />
      ) : null}
    </>
  );
}

/** Threads: the conversations of every agent, most recently active first. Filters live in the URL
 * (`agent_id`, `q`); a row opens the thread in the agent's chat. */
export function ThreadsPage() {
  const router = useRouter();
  const params = useSearchParams();
  const agentId = params?.get("agent_id") ?? "";
  const q = params?.get("q") ?? "";
  const [names, setNames] = useState<Record<string, string>>({});

  useEffect(() => {
    let live = true;
    api.agents.list().then(
      (agents) => {
        if (live) setNames(Object.fromEntries(agents.map((a) => [a.id, a.name])));
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, []);

  const agentOptions = useMemo(() => Object.entries(names).sort((a, b) => a[1].localeCompare(b[1])), [names]);
  const setParam = (patch: { agent_id?: string; q?: string }) => {
    router.replace(routes.threads({ agent_id: patch.agent_id ?? agentId, q: patch.q ?? q }));
  };

  return (
    <>
      <PageHeader crumbs={[navCrumb("threads", { current: true })]} />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <p className="text-body text-muted-foreground">The conversations of every agent. Open one to continue it in the agent&apos;s Chat tab.</p>
        <ThreadsList agentId={agentId} q={q} names={names} agentOptions={agentOptions} setParam={setParam} />
      </div>
    </>
  );
}
