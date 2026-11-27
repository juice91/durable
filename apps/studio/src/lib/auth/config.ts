// How Studio signs people in, chosen at RUNTIME by the Studio server (EAP_STUDIO_AUTH=local|zitadel|access).
//
// - `local`: the deployment's one built-in admin account (the `localadmin` plug-in): Studio shows the first-run setup
//   page while there is no admin, else asks for the username and password; the API issues the tokens, which live in
//   memory only (docs/admin-account.md). An API without the plug-in shows "No sign-in method is configured".
// - `zitadel`: Studio runs the OIDC authorization-code + PKCE flow against ZITADEL and sends `Authorization: Bearer
//   <access token>`; the tokens live in memory only.
// - `access`: Cloudflare Access in front of Studio. The browser sends no Authorization header; Access adds a JWT and the
//   API identifies the user (the `cfaccess` plug-in; lib/credentials.ts).
//
// A mode that cannot work (unset, unknown, or missing its settings) shows "No sign-in method is configured"; nothing
// falls back to another mode. The SSH-tunnel loopback page keeps the dev bearer whatever the mode says.

export interface ZitadelAuthConfig {
  mode: "zitadel";
  issuer: string;
  clientId: string;
  projectId: string;
}

export interface LocalAuthConfig {
  mode: "local";
}

export type StudioAuthConfig = ZitadelAuthConfig | LocalAuthConfig;

export interface AuthEnv {
  EAP_STUDIO_AUTH?: string;
  EAP_STUDIO_ZITADEL_ISSUER?: string;
  EAP_STUDIO_ZITADEL_CLIENT_ID?: string;
  EAP_STUDIO_ZITADEL_PROJECT_ID?: string;
  /** Local development only: the dev token a non-production build sends (lib/credentials.ts). */
  NODE_ENV?: string;
  NEXT_PUBLIC_EAP_DEV_TOKEN?: string;
}

export const DEFAULT_ISSUER = "https://auth.example-org.ai";

/**
 * - `none-needed`: Studio signs nobody in (the loopback dev bearer, `access` mode, a development token).
 * - `oidc` / `local`: Studio signs the person in with `config`.
 * - `unavailable`: nothing can sign anyone in here.
 */
export type SignIn =
  | { signIn: "none-needed" }
  | { signIn: "oidc"; config: ZitadelAuthConfig }
  | { signIn: "local"; config: LocalAuthConfig }
  | { signIn: "unavailable" };

export interface RequestFacts {
  /** The loopback page that gets the dev bearer. */
  loopbackBearer: boolean;
}

/** How this request signs in. `onProblem` gets, for the server log, why nothing can (setting names only). */
export function resolveSignIn(env: AuthEnv, facts: RequestFacts, onProblem?: (problem: string) => void): SignIn {
  if (facts.loopbackBearer) return { signIn: "none-needed" }; // the SSH path: the dev bearer, unchanged
  const mode = (env.EAP_STUDIO_AUTH ?? "").trim().toLowerCase();
  if (!mode && env.NODE_ENV !== "production" && env.NEXT_PUBLIC_EAP_DEV_TOKEN) return { signIn: "none-needed" };
  const unavailable = (problem: string): SignIn => {
    onProblem?.(problem);
    return { signIn: "unavailable" };
  };
  if (mode === "access") return { signIn: "none-needed" };
  if (mode === "local") return { signIn: "local", config: { mode: "local" } };
  if (mode !== "zitadel") return unavailable("EAP_STUDIO_AUTH must be set to local, zitadel or access");
  const issuer = (env.EAP_STUDIO_ZITADEL_ISSUER ?? DEFAULT_ISSUER).trim().replace(/\/+$/, "");
  const clientId = (env.EAP_STUDIO_ZITADEL_CLIENT_ID ?? "").trim();
  const projectId = (env.EAP_STUDIO_ZITADEL_PROJECT_ID ?? "").trim();
  const missing = [
    clientId ? "" : "EAP_STUDIO_ZITADEL_CLIENT_ID",
    projectId ? "" : "EAP_STUDIO_ZITADEL_PROJECT_ID",
    /^https?:\/\//.test(issuer) ? "" : "EAP_STUDIO_ZITADEL_ISSUER",
  ].filter(Boolean);
  if (missing.length > 0) return unavailable(`EAP_STUDIO_AUTH=zitadel needs ${missing.join(", ")}`);
  return { signIn: "oidc", config: { mode: "zitadel", issuer, clientId, projectId } };
}

/** The scopes Studio asks for: the identity, a refresh token, the project in `aud`, the roles and the org. */
export function scopesFor(projectId: string): string {
  return [
    "openid", "email", "profile", "offline_access",
    `urn:zitadel:iam:org:project:id:${projectId}:aud`,
    "urn:zitadel:iam:org:projects:roles",
    "urn:zitadel:iam:user:resourceowner",
  ].join(" ");
}
