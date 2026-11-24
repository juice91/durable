// Runs the sign-in for the page: the renew timer, the one-renew-then-login rule on a 401, and sign-out.
// Everything here is inert unless the server published a zitadel config (`window.__EAP_RUNTIME_CONFIG__.auth`).
import type { StudioAuthConfig } from "./config";
import { createOidcClient, safeReturnTo, type OidcClient } from "./oidc";
import { getTokens, setTokens } from "./session";

/** Renew when this share of the token's life is gone (a 15-minute token renews at 12 minutes), at most this early. */
const RENEW_AT = 0.8;
const MIN_RENEW_DELAY_MS = 10_000;

let client: OidcClient | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let installed = false;
let onGiveUp: ((message: string) => void) | null = null;

export function authConfig(): StudioAuthConfig | undefined {
  if (typeof window === "undefined") return undefined;
  const auth = window.__EAP_RUNTIME_CONFIG__?.auth;
  return auth && auth.mode === "zitadel" ? auth : undefined;
}

export function oidc(): OidcClient | null {
  if (client) return client;
  const config = authConfig();
  if (!config || typeof window === "undefined") return null;
  client = createOidcClient({
    config,
    fetch: (...args) => window.fetch(...args),
    storage: window.sessionStorage,
    origin: window.location.origin,
    navigate: (url) => window.location.assign(url),
  });
  return client;
}

export function resetAuthForTests(): void {
  client = null;
  installed = false;
  onGiveUp = null;
  if (timer) clearTimeout(timer);
  timer = null;
  setTokens(null);
}

/** Tell the page when sign-in cannot continue on its own (a fresh token was refused): it shows a message, not a loop. */
export function setGiveUpHandler(handler: ((message: string) => void) | null): void {
  onGiveUp = handler;
}

export async function startLogin(returnTo: string): Promise<void> {
  await oidc()?.beginLogin(safeReturnTo(returnTo));
}

function scheduleRenew(): void {
  if (timer) clearTimeout(timer);
  const tokens = getTokens();
  const auth = oidc();
  if (!tokens || !auth) return;
  const delay = Math.max(MIN_RENEW_DELAY_MS, (tokens.expiresAt - Date.now()) * RENEW_AT);
  timer = setTimeout(async () => {
    if (await auth.renew()) scheduleRenew();
    // A refused or failed renew is not retried by the timer: the next 401 runs the renew-then-login rule.
  }, delay);
}

/** Call after the tokens are set: keeps them fresh. */
export function keepFresh(): void {
  scheduleRenew();
}

function isApiUrl(url: string): boolean {
  return url.startsWith("/api/") || /^https?:\/\/[^/]+\/api\//.test(url);
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
}

/**
 * Wrap `window.fetch` once: an API call that answers 401 renews the token once and repeats with the new one; if the renew
 * fails (or the repeat is 401 again) the page goes to the login. A body that cannot be replayed (a stream) is not repeated.
 */
export function installAuthFetch(): void {
  if (installed || typeof window === "undefined" || !oidc()) return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await original(input, init);
    if (response.status !== 401 || !isApiUrl(requestUrl(input)) || getTokens() === null) return response;
    const auth = oidc();
    if (!auth) return response;
    const replayable = !(init?.body && typeof init.body === "object" && "getReader" in init.body) && !(input instanceof Request);
    if (replayable && (await auth.renew()) && getTokens()) {
      const headers = new Headers(init?.headers);
      headers.set("Authorization", `Bearer ${getTokens()!.accessToken}`);
      scheduleRenew();
      const again = await original(input, { ...init, headers });
      if (again.status !== 401) return again;
    }
    if (auth.justLoggedIn()) {
      onGiveUp?.("You are signed in, but the API refused your session. Ask an admin to check your access.");
      return response;
    }
    setTokens(null);
    void auth.beginLogin(window.location.pathname + window.location.search);
    return response;
  };
}

export async function signOut(): Promise<void> {
  if (timer) clearTimeout(timer);
  await oidc()?.logout();
}
