"use client";

import React, { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { addable, addGrant, getAccess, grantNotes, removeGrant, type AccessStatus, type GrantSubjectKind, type UseGrant } from "@/lib/agentGrants";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";

const KIND_LABEL = { mcp_server: "MCP server", secret: "Secret" } as const;

/**
 * The Access section of an agent or a workflow page (docs/iam.md, "Agent and workflow grants"): the MCP servers and secrets an
 * admin authorised it to use, who granted each and when, and for a server the credentials it carries. Everyone who may read the
 * agent or workflow sees the list; an admin also sees Add and Remove. Shown only when the policy engine keeps grants (not under
 * the transitional `openaccess` engine), and nothing is shown when the section cannot be read.
 */
export function AccessSection({
  kind,
  id,
  heading = true,
  wrapper,
}: {
  kind: GrantSubjectKind;
  id: string;
  heading?: boolean;
  /** Puts the section in its host's frame (a panel card, a config section); not called when nothing is shown. */
  wrapper?: (content: React.ReactNode) => React.ReactNode;
}) {
  const [status, setStatus] = useState<AccessStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setStatus(await getAccess(kind, id, signal));
      } catch (caught) {
        if ((caught as { name?: string }).name !== "AbortError") setStatus(null);
      }
    },
    [kind, id],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (!status || !status.supported) return null;
  const options = addable(status);

  const change = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The change was not saved.");
    } finally {
      setBusy(false);
    }
  };
  const add = () => {
    const chosen = options.find((o) => `${o.kind}:${o.name}` === pick);
    if (!chosen) return;
    setPick("");
    void change(() => addGrant(kind, id, chosen));
  };
  const remove = (grant: UseGrant) => void change(() => removeGrant(kind, id, { kind: grant.kind, name: grant.name }));
  const title = "Access";

  const section = (
    <section aria-labelledby={`access-${id}-title`} data-testid="access-section" className="min-w-0">
      {heading ? (
        <h3 id={`access-${id}-title`} className="mb-1.5 flex items-center gap-2 text-label font-semibold text-foreground">
          {title}
          {status.enforced ? null : <Badge variant="neutral">Not enforced yet</Badge>}
        </h3>
      ) : null}
      <p className="mb-2 text-caption text-muted-foreground">
        The MCP servers and secrets this {kind} may use. Runs may use only what an admin granted.
      </p>
      {status.grants.length === 0 ? (
        <p data-testid="access-empty" className="text-label text-muted-foreground">
          Nothing granted yet.
        </p>
      ) : (
        <ul className="space-y-2">
          {status.grants.map((grant) => (
            <li key={`${grant.kind}:${grant.name}`} data-testid={`access-grant-${grant.kind}-${grant.name}`} className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-1.5 text-label">
                  <Badge variant="outline">{KIND_LABEL[grant.kind]}</Badge>
                  <span className="break-all font-mono text-caption">{grant.name}</span>
                </p>
                {grantNotes(grant).map((note) => (
                  <p key={note} className="text-caption text-muted-foreground">
                    {note}
                  </p>
                ))}
              </div>
              {status.can_manage ? (
                <Button type="button" size="sm" variant="ghost" disabled={busy} aria-label={`Remove ${grant.name}`} onClick={() => remove(grant)}>
                  Remove
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {status.can_manage ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Select value={pick} onValueChange={setPick} aria-label="Add an MCP server or secret" placeholder="Add an MCP server or secret…" disabled={busy || options.length === 0} size="sm">
            {options.map((o) => (
              <option key={`${o.kind}:${o.name}`} value={`${o.kind}:${o.name}`}>
                {o.label}
              </option>
            ))}
          </Select>
          <Button type="button" size="sm" disabled={busy || pick === ""} onClick={add}>
            Add
          </Button>
        </div>
      ) : null}
      {error ? (
        <p role="alert" data-testid="access-error" className="mt-2 text-caption text-destructive-text">
          {error}
        </p>
      ) : null}
    </section>
  );
  return <>{wrapper ? wrapper(section) : section}</>;
}
