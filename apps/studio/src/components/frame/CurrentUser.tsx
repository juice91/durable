"use client";

import React from "react";
import { AccountMenu } from "@/components/account/AccountMenu";
import { Avatar } from "@/components/account/Avatar";
import { useAccount } from "@/components/account/useAccount";
import { Tooltip } from "@/components/ui/Tooltip";
import { cn } from "@/lib/cn";
import { displayName, providerLabel } from "@/lib/iam";

const focusRing = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/**
 * The signed-in user in the sidebar footer, from `GET /api/v1/iam/capabilities` (`me`). The row (or, on the rail, the
 * avatar) is the button that opens the account menu (components/account/AccountMenu): identity, Profile, account links,
 * theme and Sign out.
 *
 * Nothing is shown while loading or when the request fails (no logging): the page works without it.
 */
export function CurrentUser({ rail }: { rail: boolean }) {
  const { me, picture, expiresAt } = useAccount();
  if (!me) return null;

  const name = displayName(me);
  const loopback = me.provider === "loopback-dev";
  const shown = loopback ? `${me.subject} (loopback dev)` : name;
  const roles = me.roles.length > 0 ? ` · ${me.roles.join(", ")}` : "";
  const tip = `${providerLabel(me.provider)} · tenant ${me.tenant_id}${roles}`;

  if (rail) {
    return (
      <div data-testid="current-user" className="flex justify-center py-1">
        <AccountMenu me={me} picture={picture} expiresAt={expiresAt} side="right">
          <button type="button" aria-label={`Account menu, ${shown}`} data-testid="current-user-avatar" className={cn("rounded-full", focusRing)}>
            <Avatar name={name} src={picture} />
          </button>
        </AccountMenu>
      </div>
    );
  }
  return (
    <div data-testid="current-user" className="flex min-w-0 items-center px-0 py-1 text-label text-muted-foreground">
      <AccountMenu me={me} picture={picture} expiresAt={expiresAt} side="top">
        <button type="button" aria-label={`Account menu, ${shown}`} data-testid="current-user-trigger" className={cn("flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1 text-left hover:bg-fill-hover", focusRing)}>
          <Avatar name={name} src={picture} />
          <span className="flex min-w-0 flex-1 flex-col">
            <Tooltip content={tip} side="top" align="start">
              <span data-testid="current-user-name" className="truncate text-foreground">{shown}</span>
            </Tooltip>
            {loopback ? null : (
              <span data-testid="current-user-provider" className="w-fit rounded-sm border border-border px-1 text-caption">{providerLabel(me.provider)}</span>
            )}
          </span>
        </button>
      </AccountMenu>
    </div>
  );
}
