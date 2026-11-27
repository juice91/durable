"use client";

import React, { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { loadCapabilities, membersAreOpen, providerLabel, type IamCapabilities } from "@/lib/iam";
import {
  changeMemberRole, inviteMember, listMembers, MEMBER_ROLES, primaryRole, removeMember, type Member, type MemberRole,
} from "@/lib/members";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

const inputClass =
  "h-8 min-w-0 rounded-md border border-border bg-surface px-2 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60";

function message(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 404) return "Members are not available to you.";
    return reason.message;
  }
  return reason instanceof Error ? reason.message : "The request failed.";
}

/**
 * The Settings "Members" tab (design 7.3): the tenant's members and their roles, from the membership source (ZITADEL).
 * Invite, change a role, remove; the last admin cannot be demoted or removed (the API refuses, the message is shown).
 * Read only ("managed in <IdP>") when the source is not mutable. Under the transitional policy engine every user is
 * an admin, so the page is open to everyone and says so.
 */
export function MembersSection() {
  const [caps, setCaps] = useState<IamCapabilities | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [mutable, setMutable] = useState(false);
  const [canCreate, setCanCreate] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<MemberRole>("viewer");
  const [removing, setRemoving] = useState<Member | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await listMembers();
      setMembers(list.items);
      setMutable(list.mutable);
      setCanCreate(list.canCreateUsers);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCapabilities().then(setCaps).catch(() => undefined);
    void load();
  }, [load]);

  const run = async (key: string, action: () => Promise<unknown>, onDone?: () => void): Promise<boolean> => {
    setBusy(key);
    setError(null);
    try {
      await action();
      onDone?.();
      await load();
      return true;
    } catch (reason) {
      setError(message(reason));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const idp = caps?.membership?.name ? providerLabel(caps.membership.name) : "the identity provider";
  const builtIn = caps?.membership?.name === "localadmin";
  return (
    <section aria-labelledby="settings-members" data-testid="settings-members" className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex min-w-0 flex-col gap-1">
        <h2 id="settings-members" className="text-title font-semibold text-foreground">Members</h2>
        <p className="text-label text-muted-foreground">
          Who can sign in to this tenant and what they may do: admin (everything), editor (build and run), viewer (read).{" "}
          {builtIn ? "This deployment has one built-in admin account." : `Changes are made in ${idp}, the single source of truth.`}
        </p>
        {membersAreOpen(caps) ? (
          <p data-testid="members-open-notice" className="text-label text-muted-foreground">
            Access is open for now: every signed-in user acts as an admin until a policy engine is configured, so roles are recorded but not yet enforced.
          </p>
        ) : null}
        {!loading && !mutable && !error ? (
          <p data-testid="members-readonly" className="text-label text-muted-foreground">{builtIn ? "Read only. For several users or roles, switch this deployment to ZITADEL." : `Read only: members are managed in ${idp}.`}</p>
        ) : null}
        {mutable && !canCreate ? (
          <p data-testid="members-no-create" className="text-label text-muted-foreground">
            New users cannot be created from here for this organisation. Create the person in the {idp} console first, then add
            them here with the same e-mail address and give them a role.
          </p>
        ) : null}
      </div>
      {mutable ? (
        <form
          data-testid="members-invite"
          className="flex min-w-0 flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!email.trim()) return;
            void run("invite", () => inviteMember(email, role), () => setEmail(""));
          }}
        >
          <label className="flex min-w-0 flex-col gap-1 text-caption text-muted-foreground" htmlFor="member-email">
            E-mail
            <input id="member-email" type="email" required className={`${inputClass} w-64`} value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com" />
          </label>
          <label className="flex min-w-0 flex-col gap-1 text-caption text-muted-foreground" htmlFor="member-role">
            Role
            <select id="member-role" className={inputClass} value={role} onChange={(e) => setRole(e.target.value as MemberRole)}>
              {MEMBER_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <Button type="submit" data-testid="members-invite-submit" disabled={busy !== null || !email.trim()}>{canCreate ? "Invite" : "Add member"}</Button>
        </form>
      ) : null}
      {error ? <p role="alert" data-testid="members-error" className="text-label text-destructive-text">{error}</p> : null}
      <div className="min-w-0 overflow-x-auto">
        <table data-testid="members-table" className="w-full md:min-w-[32rem] border-collapse text-left text-label">
          <thead>
            <tr className="border-b border-border text-caption text-muted-foreground">
              <th scope="col" className="py-1.5 pr-3 font-medium">Member</th>
              <th scope="col" className="py-1.5 pr-3 font-medium">Role</th>
              <th scope="col" className="py-1.5 pr-3 font-medium">Status</th>
              <th scope="col" className="py-1.5 font-medium"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.id} data-testid="member-row" data-member-id={member.id} className="border-b border-border align-middle last:border-b-0">
                <td className="min-w-0 break-all py-1.5 pr-3">
                  <span className="text-foreground">{member.email ?? member.id}</span>
                  {member.display_name && member.display_name !== member.email ? <span className="block text-caption text-muted-foreground">{member.display_name}</span> : null}
                </td>
                <td className="py-1.5 pr-3">
                  {mutable ? (
                    <select
                      aria-label={`Role of ${member.email ?? member.id}`}
                      data-testid="member-role"
                      className={inputClass}
                      value={primaryRole(member)}
                      disabled={busy !== null}
                      onChange={(e) => void run(member.id, () => changeMemberRole(member.id, e.target.value as MemberRole))}
                    >
                      {primaryRole(member) === "" ? <option value="" disabled>none</option> : null}
                      {MEMBER_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  ) : (
                    <span data-testid="member-role-text">{member.roles.join(", ") || "none"}</span>
                  )}
                </td>
                <td className="py-1.5 pr-3"><Badge variant={member.status === "active" ? "success" : "warning"}>{member.status}</Badge></td>
                <td className="py-1.5 text-right">
                  {mutable ? (
                    <Button type="button" variant="ghost" data-testid="member-remove" disabled={busy !== null} onClick={() => { setRemoveError(null); setRemoving(member); }}>Remove</Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {loading ? <p data-testid="members-loading" className="text-label text-muted-foreground">Loading…</p> : null}
      {!loading && !error && members.length === 0 ? <p data-testid="members-empty" className="text-label text-muted-foreground">No members yet.</p> : null}
      {removing ? (
        <ConfirmDialog
          title={`Remove ${removing.email ?? removing.id}?`}
          description="They lose access to this tenant at once. The last admin cannot be removed."
          confirmLabel="Remove"
          destructive
          busy={busy === removing.id}
          error={removeError}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const target = removing;
            setBusy(target.id);
            setRemoveError(null);
            removeMember(target.id)
              .then(async () => {
                setRemoving(null);
                await load();
              })
              .catch((reason) => setRemoveError(message(reason)))
              .finally(() => setBusy(null));
          }}
        />
      ) : null}
    </section>
  );
}
