// The OIDC authorization-code + PKCE client for ZITADEL (design 1.6, option a). A factory with injectable browser
// pieces, so the whole flow is testable against an in-process provider. No token is ever logged or put in storage;
// only the one-time `state` / verifier / nonce of a login in flight live in sessionStorage across the redirect.
import { codeChallenge, randomString } from "./pkce";
import { scopesFor, type StudioAuthConfig } from "./config";
import { setTokens, getTokens, type Tokens } from "./session";

export const CALLBACK_PATH = "/auth/callback";
const PENDING_KEY = "eap.oidc.pending";
const LAST_LOGIN_KEY = "eap.oidc.lastLogin";
/** A second login redirect within this window means the API refused a fresh token: stop instead of looping. */
export const LOGIN_LOOP_WINDOW_MS = 15_000;

export class AuthFlowError extends Error {}

interface Discovery {
  authorization_endpoint: string;
  token_endpoint: string;
  end_session_endpoint?: string;
}

interface TokenResponse {
  access_token?: string;
  refresh_token?: string;
  id_token?: string;
  expires_in?: number;
}

interface Pending {
  state: string;
  verifier: string;
  nonce: string;
  returnTo: string;
}

export interface OidcDeps {
  config: StudioAuthConfig;
  fetch: typeof fetch;
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  origin: string;
  navigate: (url: string) => void;
  now?: () => number;
}

function jwtPayload(token: string): Record<string, unknown> {
  try {
    const body = token.split(".")[1] ?? "";
    const padded = body.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (body.length % 4)) % 4);
    return JSON.parse(atob(padded)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** Only a same-origin path may be a return target (never an open redirect). */
export function safeReturnTo(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\") || value.startsWith(CALLBACK_PATH)) return "/";
  return value;
}

export function createOidcClient(deps: OidcDeps) {
  const { config, storage, origin } = deps;
  const now = deps.now ?? (() => Date.now());
  const redirectUri = `${origin}${CALLBACK_PATH}`;
  let discovery: Promise<Discovery> | null = null;
  let renewing: Promise<boolean> | null = null;

  function discover(): Promise<Discovery> {
    discovery ??= (async () => {
      const response = await deps.fetch(`${config.issuer}/.well-known/openid-configuration`, { cache: "no-store" });
      if (!response.ok) throw new AuthFlowError("The identity provider is not reachable.");
      const doc = (await response.json()) as Partial<Discovery>;
      if (!doc.authorization_endpoint || !doc.token_endpoint) throw new AuthFlowError("The identity provider's configuration is incomplete.");
      return doc as Discovery;
    })();
    discovery.catch(() => {
      discovery = null; // a failure is not remembered
    });
    return discovery;
  }

  function store(response: TokenResponse, previous: Tokens | null): Tokens {
    if (!response.access_token) throw new AuthFlowError("The identity provider returned no access token.");
    const tokens: Tokens = {
      accessToken: response.access_token,
      refreshToken: response.refresh_token ?? previous?.refreshToken,
      idToken: response.id_token ?? previous?.idToken,
      expiresAt: now() + (response.expires_in ?? 300) * 1000,
    };
    setTokens(tokens);
    return tokens;
  }

  async function tokenRequest(form: Record<string, string>): Promise<TokenResponse | null> {
    const { token_endpoint } = await discover();
    const response = await deps.fetch(token_endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: new URLSearchParams({ ...form, client_id: config.clientId }).toString(),
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as TokenResponse;
  }

  return {
    redirectUri,

    /** Start a sign-in: remember the one-time values, then send the browser to ZITADEL. */
    async beginLogin(returnTo: string): Promise<void> {
      const { authorization_endpoint } = await discover();
      const pending: Pending = { state: randomString(16), verifier: randomString(32), nonce: randomString(16), returnTo: safeReturnTo(returnTo) };
      storage.setItem(PENDING_KEY, JSON.stringify(pending));
      storage.setItem(LAST_LOGIN_KEY, String(now()));
      const url = new URL(authorization_endpoint);
      url.search = new URLSearchParams({
        response_type: "code",
        client_id: config.clientId,
        redirect_uri: redirectUri,
        scope: scopesFor(config.projectId),
        state: pending.state,
        nonce: pending.nonce,
        code_challenge: await codeChallenge(pending.verifier),
        code_challenge_method: "S256",
      }).toString();
      deps.navigate(url.toString());
    },

    /** True when a login redirect just happened: a 401 right after it must not start another one. */
    justLoggedIn(): boolean {
      const at = Number(storage.getItem(LAST_LOGIN_KEY));
      return Number.isFinite(at) && at > 0 && now() - at < LOGIN_LOOP_WINDOW_MS;
    },

    /** The page at /auth/callback: check `state`, exchange the code, keep the tokens in memory. Returns where to go next. */
    async completeLogin(search: string): Promise<string> {
      const params = new URLSearchParams(search);
      const raw = storage.getItem(PENDING_KEY);
      storage.removeItem(PENDING_KEY); // a code is used once
      const error = params.get("error");
      if (error) throw new AuthFlowError(params.get("error_description") || `Sign-in was refused (${error}).`);
      const pending = raw ? (JSON.parse(raw) as Pending) : null;
      const code = params.get("code");
      if (!pending || !code || params.get("state") !== pending.state) throw new AuthFlowError("The sign-in response does not match this browser's request. Try again.");
      const response = await tokenRequest({ grant_type: "authorization_code", code, redirect_uri: redirectUri, code_verifier: pending.verifier });
      if (!response) throw new AuthFlowError("The identity provider refused the sign-in code.");
      if (response.id_token && jwtPayload(response.id_token).nonce !== pending.nonce) throw new AuthFlowError("The sign-in response failed its nonce check.");
      store(response, null);
      return safeReturnTo(pending.returnTo);
    },

    /** Silent renew with the rotating refresh token. One renew at a time; false when it cannot (no token, refused). */
    renew(): Promise<boolean> {
      renewing ??= (async () => {
        const refreshToken = getTokens()?.refreshToken;
        if (!refreshToken) return false;
        try {
          const response = await tokenRequest({ grant_type: "refresh_token", refresh_token: refreshToken });
          if (!response) {
            setTokens(null);
            return false;
          }
          store(response, getTokens());
          return true;
        } catch {
          return false; // a network error: the tokens stay, the next 401 or timer tries again
        }
      })().finally(() => {
        renewing = null;
      });
      return renewing;
    },

    /** Forget the tokens and end ZITADEL's session; it sends the browser back to Studio's own origin. */
    async logout(): Promise<void> {
      const idToken = getTokens()?.idToken;
      setTokens(null);
      storage.removeItem(PENDING_KEY);
      let endpoint: string | undefined;
      try {
        endpoint = (await discover()).end_session_endpoint;
      } catch {
        endpoint = undefined;
      }
      if (!endpoint) {
        deps.navigate(`${origin}/`);
        return;
      }
      const url = new URL(endpoint);
      const params = new URLSearchParams({ client_id: config.clientId, post_logout_redirect_uri: `${origin}/` });
      if (idToken) params.set("id_token_hint", idToken);
      url.search = params.toString();
      deps.navigate(url.toString());
    },
  };
}

export type OidcClient = ReturnType<typeof createOidcClient>;
