// What the account menu and the Profile page say about the signed-in person. Names and labels only, never a token.
import { authConfig } from "./auth/controller";
import type { IamMe } from "./iam";

/** The issuer's self-service console page for the signed-in user (ZITADEL docs, "Self-service": `<custom domain>/ui/console/users/me`).
 * Password, second factor and passkeys are tabs on that one page; the console is separate from the login UI, so it
 * does not depend on the instance using login v1. */
export function accountConsoleUrl(): string | null {
  const config = authConfig();
  return config ? `${config.issuer}/ui/console/users/me` : null;
}

export interface AccountLink {
  id: "manage" | "password" | "two-factor";
  label: string;
  href: string;
}

/** ZITADEL users only; empty for every other sign-in (the SSH loopback page, Cloudflare Access, API keys). */
export function accountLinks(me: IamMe): AccountLink[] {
  const href = me.provider === "zitadel" ? accountConsoleUrl() : null;
  if (!href) return [];
  return [
    { id: "manage", label: "Manage account", href },
    { id: "password", label: "Change password", href },
    { id: "two-factor", label: "Two-factor", href },
  ];
}

export function signInMethod(provider: string): string {
  if (provider === "zitadel") return "ZITADEL single sign-on";
  if (provider === "localadmin") return "Built-in admin account";
  if (provider === "cf-access") return "Cloudflare Access";
  if (provider === "loopback-dev") return "Dev bearer (SSH loopback)";
  if (provider === "api-key" || provider === "apikey") return "API key";
  return provider || "Unknown";
}

export const ROLE_MEANING: Record<string, string> = {
  admin: "Manages the tenant: members, secrets, API keys and the audit log, plus everything an editor does.",
  editor: "Builds and runs agents, workflows and knowledge bases.",
  viewer: "Read-only: looks at agents, runs and results but changes nothing.",
};

export function roleMeaning(role: string): string {
  return ROLE_MEANING[role] ?? "A role defined outside Studio.";
}

/** "14:32" today, otherwise the local date and time. */
export function formatUntil(expiresAt: number, now: number = Date.now()): string {
  const at = new Date(expiresAt);
  const sameDay = at.toDateString() === new Date(now).toDateString();
  return sameDay ? at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : at.toLocaleString();
}
