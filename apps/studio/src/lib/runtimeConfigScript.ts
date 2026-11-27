import { resolveSignIn, type AuthEnv, type SignIn } from "./auth/config";
import type { RuntimeConfig } from "./base-url";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

/** The hostname of a Host header (no port), lower-cased. */
export function hostName(host: string | null): string {
  const value = (host ?? "").trim().toLowerCase();
  if (value.startsWith("[")) return value.slice(0, value.indexOf("]") + 1);
  return value.split(":")[0];
}

export interface RuntimeSettings {
  config: RuntimeConfig;
  /** What the root layout does: show the app, hold it behind a sign-in, or say there is no way to sign in. */
  signIn: SignIn["signIn"];
  /** The deployment has the built-in admin: /setup creates it. */
  localSetup: boolean;
}

/**
 * Runtime (not build-time) settings for the browser: the loopback API URL of the SSH tunnel, the dev bearer for that same
 * loopback page, and the sign-in settings. The bearer is included only when the request's Host is a loopback name (the
 * tunnel), never through the public host, and the browser honours it only on a loopback page (lib/credentials.ts). On
 * that loopback page the dev bearer wins: no sign-in settings are published for it. Nothing here is in the build, and
 * nothing secret is published (an OIDC public client has no secret).
 */
export function runtimeSettings(host: string | null, env: Record<string, string | undefined> = process.env): RuntimeSettings {
  const config: RuntimeConfig = { loopbackApiUrl: env.EAP_STUDIO_LOOPBACK_API_URL ?? "" };
  const bearer = env.EAP_STUDIO_LOOPBACK_DEV_BEARER;
  const loopbackBearer = Boolean(bearer) && LOOPBACK_HOSTS.has(hostName(host));
  if (bearer && loopbackBearer) config.devBearer = bearer;
  const resolved = resolveSignIn(env as AuthEnv, { loopbackBearer }, (problem) =>
    console.warn(`No sign-in method is configured: ${problem}`),
  );
  if (resolved.signIn === "oidc" || resolved.signIn === "local") config.auth = resolved.config;
  const localSetup = (env.EAP_STUDIO_AUTH ?? "").trim().toLowerCase() === "local";
  if (localSetup) config.localSetup = true;
  return { config, signIn: resolved.signIn, localSetup };
}

/** The inline script: JSON in a script element, with `<` escaped so a value can never close the tag. */
export function runtimeConfigScript(config: RuntimeConfig): string {
  return `window.__EAP_RUNTIME_CONFIG__=${JSON.stringify(config).replace(/</g, "\\u003c")};`;
}
