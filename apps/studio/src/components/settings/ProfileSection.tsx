"use client";

import React, { useEffect, useState } from "react";
import { Avatar } from "@/components/account/Avatar";
import { useAccount } from "@/components/account/useAccount";
import { Badge } from "@/components/ui/Badge";
import { accountLinks, formatUntil, roleMeaning, signInMethod } from "@/lib/account";
import { actorLabel, EMPTY_AUDIT_FILTERS, listAudit, type AuditRow } from "@/lib/audit";
import { displayName, providerLabel } from "@/lib/iam";

function Block({ id, testId, title, description, children }: { id: string; testId: string; title: string; description?: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={`profile-${id}`} data-testid={testId} className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex min-w-0 flex-col gap-1">
        <h2 id={`profile-${id}`} className="text-title font-semibold text-foreground">{title}</h2>
        {description ? <p className="text-label text-muted-foreground">{description}</p> : null}
      </div>
      {children}
    </section>
  );
}

function Facts({ rows }: { rows: { term: string; value: React.ReactNode; testId: string }[] }) {
  return (
    <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-label">
      {rows.map(({ term, value, testId }) => (
        <React.Fragment key={term}>
          <dt className="text-muted-foreground">{term}</dt>
          <dd data-testid={testId} className="min-w-0 break-words text-foreground">{value}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

/** The signed-in user's own audit rows (`actor=me`: any role may read them). */
function RecentActivity() {
  const [rows, setRows] = useState<AuditRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    listAudit({ ...EMPTY_AUDIT_FILTERS, actor: "me" }, null)
      .then((page) => live && setRows(page.items.slice(0, 10)))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, []);
  return (
    <Block id="activity" testId="profile-activity" title="My recent activity" description="The latest changes recorded under your name in this tenant.">
      {failed ? (
        <p role="status" className="text-label text-muted-foreground">Your activity could not be loaded.</p>
      ) : rows === null ? (
        <p role="status" className="text-label text-muted-foreground">Loading…</p>
      ) : rows.length === 0 ? (
        <p data-testid="profile-activity-empty" className="text-label text-muted-foreground">Nothing recorded yet.</p>
      ) : (
        <ul data-testid="profile-activity-list" className="flex flex-col divide-y divide-border text-label">
          {rows.map((row) => (
            <li key={row.id} className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 py-1.5">
              <span className="font-mono text-foreground">{row.action}</span>
              {row.resource_kind ? <span className="min-w-0 truncate text-muted-foreground">{row.resource_kind}{row.resource_id ? ` ${row.resource_id}` : ""}</span> : null}
              {row.outcome !== "success" ? <Badge variant={row.outcome === "denied" ? "warning" : "destructive"}>{row.outcome}</Badge> : null}
              <time dateTime={row.at} className="ml-auto text-caption text-muted-foreground">{new Date(row.at).toLocaleString()}</time>
              <span className="sr-only">{actorLabel(row)}</span>
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}

/** Settings → Profile: who you are, your roles, this session, the account pages and your recent activity. */
export function ProfileSection() {
  const { me, picture, expiresAt } = useAccount();
  if (!me) return <p role="status" className="text-label text-muted-foreground">Loading your profile…</p>;
  const name = displayName(me);
  const links = accountLinks(me);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Block id="identity" testId="profile-identity" title="Identity">
        <div className="flex min-w-0 items-center gap-3">
          <Avatar name={name} src={picture} size="lg" />
          <Facts
            rows={[
              { term: "Name", value: name, testId: "profile-name" },
              { term: "E-mail", value: me.email ?? "Not set", testId: "profile-email" },
              { term: "Subject", value: <span key="s" className="font-mono">{me.subject}</span>, testId: "profile-subject" },
              { term: "Provider", value: providerLabel(me.provider), testId: "profile-provider" },
              { term: "Tenant", value: <span key="t" className="font-mono">{me.tenant_id}</span>, testId: "profile-tenant" },
            ]}
          />
        </div>
      </Block>
      <Block id="roles" testId="profile-roles" title="Roles" description="Roles are recorded with your account; they are not enforced until a policy engine replaces the transitional one.">
        {me.roles.length === 0 ? (
          <p className="text-label text-muted-foreground">No roles.</p>
        ) : (
          <ul className="flex flex-col gap-2 text-label">
            {me.roles.map((role) => (
              <li key={role} data-testid={`profile-role-${role}`} className="flex min-w-0 flex-col gap-0.5">
                <span><Badge>{role}</Badge></span>
                <span className="text-muted-foreground">{roleMeaning(role)}</span>
              </li>
            ))}
          </ul>
        )}
      </Block>
      <Block id="session" testId="profile-session" title="Current session">
        <Facts
          rows={[
            { term: "Sign-in method", value: signInMethod(me.provider), testId: "profile-method" },
            ...(expiresAt !== null ? [{ term: "Signed in until", value: `${formatUntil(expiresAt)} (Studio renews it while you work)`, testId: "profile-expiry" }] : []),
          ]}
        />
      </Block>
      {links.length > 0 ? (
        <Block id="account" testId="profile-account" title="Account" description="Opens your ZITADEL account page in a new tab.">
          <ul className="flex flex-wrap gap-x-4 gap-y-1 text-label">
            {links.map((link) => (
              <li key={link.id}>
                <a href={link.href} target="_blank" rel="noopener noreferrer" data-testid={`profile-link-${link.id}`} className="text-foreground underline underline-offset-4 hover:no-underline">
                  {link.label}<span className="sr-only"> (opens in a new tab)</span>
                </a>
              </li>
            ))}
          </ul>
        </Block>
      ) : null}
      <RecentActivity />
      {/* SLOT "API keys": a later change adds personal API keys here. Renders nothing until then. */}
      <div data-slot="profile-api-keys" data-testid="profile-api-keys-slot" />
    </div>
  );
}
