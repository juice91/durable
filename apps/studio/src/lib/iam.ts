// Who is signed in and what the API's IAM plug-ins offer: GET /api/v1/iam/capabilities (design 5.7, 7.1).
//
// Studio renders from this answer once per session (a 401 or 403 refreshes it): the header user, and which pages to show.
// The server remains the enforcer; a hidden page is a convenience, not a control. Names and flags only.
import { ApiError, apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { rememberSubject } from "./subjects";

export interface IamMe {
  subject: string;
  email: string | null;
  tenant_id: string;
  kind: string;
  roles: string[];
  provider: string;
}

export interface IamCapabilities {
  mode: string;
  identity: { name: string; kinds: string[] }[];
  policy: { name: string; transitional: boolean; features: string[] };
  membership: { name: string; mutable: boolean } | null;
  me: IamMe;
  actions: string[];
}

let cached: Promise<IamCapabilities> | null = null;
let answered: IamCapabilities | null = null;

/** The last capabilities answer, without asking (null before the first one). */
export function peekCapabilities(): IamCapabilities | null {
  return answered;
}

/** The capabilities answer, fetched once and shared; `refresh` forgets it (after a 401 or 403). */
export function loadCapabilities(refresh = false): Promise<IamCapabilities> {
  if (refresh || cached === null) {
    cached = (async () => {
      const response = await fetch(`${BASE_URL}/api/v1/iam/capabilities`, { headers: apiHeaders(), cache: "no-store" });
      if (!response.ok) throw await apiErrorFromResponse(response);
      const body = (await response.json()) as IamCapabilities | null;
      if (!body || typeof body !== "object" || !body.me || typeof body.me.subject !== "string") throw new ApiError(0, "Unexpected capabilities answer");
      rememberSubject(body.me.subject, body.me.email);
      answered = { ...body, me: { ...body.me, roles: body.me.roles ?? [] }, actions: body.actions ?? [], policy: body.policy ?? { name: "", transitional: false, features: [] } };
      return answered;
    })();
    cached.catch(() => {
      cached = null; // a failure is not remembered: the next ask tries again
    });
  }
  return cached;
}

export function resetCapabilitiesForTests(): void {
  cached = null;
  answered = null;
}

/** The name to show for the signed-in principal: `me.email`, else the `subject` (an e-mail from Access, or "operator" on loopback). */
export function displayName(me: IamMe): string {
  return me.email?.trim() || me.subject;
}

/** The short provider badge: Access, Loopback dev, API key; an unknown provider shows as it is named. */
export function providerLabel(provider: string): string {
  if (provider === "cf-access") return "Access";
  if (provider === "zitadel") return "ZITADEL";
  if (provider === "localadmin") return "Built-in admin";
  if (provider === "loopback-dev") return "Loopback dev";
  if (provider === "api-key" || provider === "apikey") return "API key";
  return provider || "Unknown";
}

/** Members and Access are shown only when a plug-in offers them (design 7.2); with the transition plug-ins neither is. */
export function hasMembers(caps: IamCapabilities | null): boolean {
  return Boolean(caps?.membership) && Boolean(caps?.actions.includes("tenant.manage_members"));
}

export function hasAccessTab(caps: IamCapabilities | null): boolean {
  return Boolean(caps?.policy.features.includes("grants"));
}

/** The Audit page is core: shown whenever the caller may read it (capabilities unreadable: shown, the API refuses). */
export function canReadAudit(caps: IamCapabilities | null): boolean {
  return caps === null || caps.actions.includes("tenant.read_audit");
}

/** Under the transitional policy engine every signed-in user is an admin: the Members page is shown to everyone and labelled. */
export function membersAreOpen(caps: IamCapabilities | null): boolean {
  return Boolean(caps?.policy.transitional);
}

/** Cloudflare Access's sign-out path on the page's own origin; only for a person who signed in through Access. */
export const ACCESS_LOGOUT_PATH = "/cdn-cgi/access/logout";
export function canSignOut(me: IamMe | null): boolean {
  return me?.provider === "cf-access";
}

/** A person Studio itself signed in (ZITADEL, or the built-in admin) signs out through Studio's session (lib/auth). */
export function canSignOutStudio(me: IamMe | null): boolean {
  return me?.provider === "zitadel" || me?.provider === "localadmin";
}
