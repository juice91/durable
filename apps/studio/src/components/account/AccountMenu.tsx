"use client";

import React from "react";
import Link from "next/link";
import { ExternalLink, LogOut, Monitor, Moon, Sun, User } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/DropdownMenu";
import { useTheme } from "@/components/theme/ThemeProvider";
import { accountLinks, formatUntil, signInMethod } from "@/lib/account";
import { signOut } from "@/lib/auth/controller";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { ACCESS_LOGOUT_PATH, canSignOut, canSignOutZitadel, displayName, providerLabel, type IamMe } from "@/lib/iam";
import { routes } from "@/lib/routes";
import type { ThemePreference } from "@/lib/theme";
import { Avatar } from "./Avatar";

const icon = { strokeWidth: ICON_STROKE_WIDTH, "aria-hidden": true } as const;
const THEMES: { value: ThemePreference; label: string; Icon: typeof Sun }[] = [
  { value: "system", label: "System", Icon: Monitor },
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
];

/**
 * The account menu: who you are (avatar, name, e-mail, provider, roles, tenant, sign-in method, ZITADEL session expiry),
 * then Profile, the issuer's account pages (ZITADEL only), the theme and Sign out. The trigger is the caller's element
 * (`children`, one button); Radix gives Enter/Space/arrow keys to open, Escape to close and returns focus to it.
 */
export function AccountMenu({
  me, picture, expiresAt, side, children,
}: { me: IamMe; picture: string | null; expiresAt: number | null; side: "top" | "right"; children: React.ReactNode }) {
  const { theme, setTheme } = useTheme();
  const name = displayName(me);
  const loopback = me.provider === "loopback-dev";
  const links = accountLinks(me);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{children}</DropdownMenuTrigger>
      <DropdownMenuContent side={side} align="start" className="w-72" data-testid="account-menu">
        <div className="flex min-w-0 items-center gap-3 px-2 py-2">
          <Avatar name={name} src={picture} size="lg" />
          <div className="flex min-w-0 flex-col gap-0.5">
            <span data-testid="account-name" className="truncate text-body font-medium">{loopback ? me.subject : name}</span>
            {me.email && me.email !== name ? <span className="truncate text-caption text-muted-foreground">{me.email}</span> : null}
            <Badge variant="outline" className="w-fit" data-testid="account-provider">{providerLabel(me.provider)}</Badge>
          </div>
        </div>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 px-2 pb-2 text-caption">
          <dt className="text-muted-foreground">Roles</dt>
          <dd data-testid="account-roles" className="flex min-w-0 flex-wrap gap-1">
            {me.roles.length > 0 ? me.roles.map((role) => <Badge key={role}>{role}</Badge>) : <span className="text-muted-foreground">None</span>}
          </dd>
          <dt className="text-muted-foreground">Tenant</dt>
          <dd data-testid="account-tenant" className="min-w-0 truncate font-mono">{me.tenant_id}</dd>
          <dt className="text-muted-foreground">Sign-in</dt>
          <dd data-testid="account-method" className="min-w-0 truncate">{signInMethod(me.provider)}</dd>
          {expiresAt !== null ? (
            <>
              <dt className="text-muted-foreground">Session</dt>
              <dd data-testid="account-expiry" className="min-w-0">Signed in until {formatUntil(expiresAt)}</dd>
            </>
          ) : null}
        </dl>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link href={routes.settingsTab("profile")} data-testid="account-profile"><User {...icon} />Profile</Link>
        </DropdownMenuItem>
        {links.map((link) => (
          <DropdownMenuItem key={link.id} asChild>
            <a href={link.href} target="_blank" rel="noopener noreferrer" data-testid={`account-link-${link.id}`}>
              <ExternalLink {...icon} />{link.label}<span className="sr-only"> (opens in a new tab)</span>
            </a>
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(value) => setTheme(value as ThemePreference)}>
          {THEMES.map(({ value, label, Icon }) => (
            <DropdownMenuRadioItem key={value} value={value} data-testid={`account-theme-${value}`}>
              <Icon {...icon} />{label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        {canSignOut(me) || canSignOutZitadel(me) ? (
          <>
            <DropdownMenuSeparator />
            {canSignOut(me) ? (
              <DropdownMenuItem asChild>
                <a href={ACCESS_LOGOUT_PATH} data-testid="current-user-signout"><LogOut {...icon} />Sign out</a>
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem onSelect={() => void signOut()} data-testid="current-user-signout"><LogOut {...icon} />Sign out</DropdownMenuItem>
            )}
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
