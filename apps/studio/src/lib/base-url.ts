import type { StudioAuthConfig } from "./auth/config";

/**
 * Where the browser reaches the API. Unset keeps the local-development default; an EMPTY value means
 * same-origin (requests go to `/api/v1/...` on the origin that served Studio), which is how the public
 * hostname behind an ingress and the Next.js `/api/:path*` rewrite (next.config.ts) work.
 *
 * Exception, chosen at RUNTIME: when Studio is served from a loopback origin (the SSH tunnel to the studio pod,
 * `http://127.0.0.1:13000`) the Next rewrite would reach the API from the studio pod, which the API does not trust.
 * The Studio server then publishes `window.__EAP_RUNTIME_CONFIG__.loopbackApiUrl` (from EAP_STUDIO_LOOPBACK_API_URL,
 * read at request time, never baked in) and the browser calls that URL, the tunnel's own forward to the API pod,
 * directly. Only a loopback URL is ever honoured, and only on a loopback page.
 */
export function resolveBaseUrl(raw: string | undefined): string {
  if (raw === undefined) return "http://localhost:8000";
  return raw.trim().replace(/\/+$/, "");
}

export interface RuntimeConfig {
  loopbackApiUrl?: string;
  /** The dev bearer for a loopback page, published at runtime by the Studio server (see credentials.ts). */
  devBearer?: string;
  /** How Studio signs in under `EAP_STUDIO_AUTH=zitadel` or `local` (lib/auth/config.ts); absent otherwise. */
  auth?: StudioAuthConfig;
  /** `EAP_STUDIO_AUTH=local`: the /setup page works even where another credential wins (the loopback page). */
  localSetup?: boolean;
}

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost"]);

/** The tunnel's API origin when `raw` is `http://127.0.0.1|localhost[:port]` and nothing else, else undefined. */
export function parseLoopbackApiUrl(raw: string | undefined): string | undefined {
  if (!raw) return undefined;
  try {
    const url = new URL(raw.trim());
    const plain = url.pathname === "/" && !url.search && !url.hash && !url.username && !url.password;
    if (url.protocol !== "http:" || !LOOPBACK_HOSTS.has(url.hostname) || !plain) return undefined;
    return `${url.protocol}//${url.host}`;
  } catch {
    return undefined;
  }
}

export function chooseBaseUrl(opts: { raw: string | undefined; hostname?: string; runtime?: RuntimeConfig }): string {
  const loopback = opts.hostname !== undefined && LOOPBACK_HOSTS.has(opts.hostname);
  const direct = loopback ? parseLoopbackApiUrl(opts.runtime?.loopbackApiUrl) : undefined;
  return direct ?? resolveBaseUrl(opts.raw);
}

declare global {
  interface Window {
    __EAP_RUNTIME_CONFIG__?: RuntimeConfig;
  }
}

export const BASE_URL = chooseBaseUrl({
  raw: process.env.NEXT_PUBLIC_EAP_API_URL,
  hostname: typeof window === "undefined" ? undefined : window.location.hostname,
  runtime: typeof window === "undefined" ? undefined : window.__EAP_RUNTIME_CONFIG__,
});
