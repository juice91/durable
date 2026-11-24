// How Studio signs people in, chosen at RUNTIME by the Studio server (EAP_STUDIO_AUTH=access|zitadel, default access).
//
// - `access` (default, and today's behaviour): the browser sends no Authorization header on the public host; Cloudflare
//   Access adds a JWT and the API identifies the user (lib/credentials.ts).
// - `zitadel`: Studio runs the OIDC authorization-code + PKCE flow against ZITADEL and sends `Authorization: Bearer
//   <access token>`; the tokens live in memory only.
//
// The SSH-tunnel loopback page keeps the dev bearer whatever the mode says: it never starts a login.

export interface StudioAuthConfig {
  mode: "zitadel";
  issuer: string;
  clientId: string;
  projectId: string;
}

export interface AuthEnv {
  EAP_STUDIO_AUTH?: string;
  EAP_STUDIO_ZITADEL_ISSUER?: string;
  EAP_STUDIO_ZITADEL_CLIENT_ID?: string;
  EAP_STUDIO_ZITADEL_PROJECT_ID?: string;
}

export const DEFAULT_ISSUER = "https://auth.example-org.ai";

/**
 * The OIDC settings for this request, or undefined for `access`. A deployment that asks for `zitadel` but has not
 * given Studio its client id and project id stays on `access` (a half-set switch must not lock everybody out), and
 * `onIncomplete` says so.
 */
export function resolveAuthConfig(env: AuthEnv, loopbackBearer: boolean, onIncomplete?: (missing: string[]) => void): StudioAuthConfig | undefined {
  if ((env.EAP_STUDIO_AUTH ?? "access").trim().toLowerCase() !== "zitadel") return undefined;
  if (loopbackBearer) return undefined; // the SSH path: the dev bearer, unchanged
  const issuer = (env.EAP_STUDIO_ZITADEL_ISSUER ?? DEFAULT_ISSUER).trim().replace(/\/+$/, "");
  const clientId = (env.EAP_STUDIO_ZITADEL_CLIENT_ID ?? "").trim();
  const projectId = (env.EAP_STUDIO_ZITADEL_PROJECT_ID ?? "").trim();
  const missing = [clientId ? "" : "EAP_STUDIO_ZITADEL_CLIENT_ID", projectId ? "" : "EAP_STUDIO_ZITADEL_PROJECT_ID"].filter(Boolean);
  if (missing.length > 0 || !/^https?:\/\//.test(issuer)) {
    onIncomplete?.(missing.length > 0 ? missing : ["EAP_STUDIO_ZITADEL_ISSUER"]);
    return undefined;
  }
  return { mode: "zitadel", issuer, clientId, projectId };
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
