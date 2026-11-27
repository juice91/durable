/**
 * What Studio sends to the API to say who it is. No credential is built into the bundle.
 *
 * - With `EAP_STUDIO_AUTH=access` the browser sends no `Authorization` header on the public host:
 *   the request carries the Cloudflare Access cookie (same origin) and cloudflared adds the signed Access JWT, which
 *   only a single-tenant API that lists the `eap-iam-cfaccess` provider identifies.
 * - With `EAP_STUDIO_AUTH=local` the built-in admin signs in (lib/auth/local.ts) and the page sends its bearer.
 * - On a loopback page (the SSH tunnel at `http://127.0.0.1:13000`) the Studio server publishes a dev bearer as a
 *   RUNTIME value (`window.__EAP_RUNTIME_CONFIG__.devBearer`, from EAP_STUDIO_LOOPBACK_DEV_BEARER, only for a request
 *   whose Host is a loopback name) and the API accepts it only from a loopback TCP peer. It is honoured here only
 *   on a loopback page, whatever the config says.
 * - With `EAP_STUDIO_AUTH=zitadel` the page signs in through ZITADEL (lib/auth) and sends `Bearer <access token>`; the
 *   loopback page above still uses the dev bearer.
 * - Development and tests (`NODE_ENV !== "production"`) may still set `NEXT_PUBLIC_EAP_DEV_TOKEN`; that branch is
 *   removed from a production build, so the variable can never reach the bundle.
 */

import type { RuntimeConfig } from "./base-url";
import { getAccessToken } from "./auth/session";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost"]);

/** The runtime dev bearer when the page really is on a loopback origin, else undefined. */
export function runtimeDevBearer(hostname: string | undefined, runtime: RuntimeConfig | undefined): string | undefined {
  const value = runtime?.devBearer;
  if (!value || hostname === undefined || !LOOPBACK_HOSTS.has(hostname)) return undefined;
  return value.startsWith("Bearer ") ? value : undefined;
}

/** The `Authorization` value to send, or undefined when the request should carry only the Access cookie. */
export function authorizationValue(): string | undefined {
  if (process.env.NODE_ENV !== "production" && !getAccessToken()) {
    // A signed-in session (a sign-in mode under development) wins over the development token.
    const fromEnv = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
    if (fromEnv) return fromEnv;
  }
  if (typeof window === "undefined") return undefined;
  const loopback = runtimeDevBearer(window.location.hostname, window.__EAP_RUNTIME_CONFIG__);
  if (loopback) return loopback;
  // EAP_STUDIO_AUTH=zitadel: the access token of the signed-in person (memory only; see lib/auth).
  const token = getAccessToken();
  return token ? `Bearer ${token}` : undefined;
}

/** `{ Authorization }` when there is one, else `{}`. */
export function authorizationHeaders(): Record<string, string> {
  const value = authorizationValue();
  return value ? { Authorization: value } : {};
}
