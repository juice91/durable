// The built-in admin account (EAP_STUDIO_AUTH=local): the username and password go to the API, which answers with the
// same pair ZITADEL would (a short-lived access token and a rotating refresh token), kept in memory only (session.ts).
// No password is stored or logged. No cookie is involved, so there is nothing for another site to ride on.
import { BASE_URL } from "../base-url";
import { getTokens, setTokens } from "./session";

const PATH = "/api/v1/auth/local";
/** Studio's first-run page; the deploy script links to it as `/setup#code=…`. */
export const SETUP_PATH = "/setup";
/** A refused session within this window of a sign-in is not asked for again: the API refuses fresh tokens. */
const SIGN_IN_LOOP_WINDOW_MS = 15_000;

/** What the sign-in form shows. */
export class LocalAuthError extends Error {}

interface SessionAnswer {
  password_change_required?: boolean;
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
}

export interface LocalDeps {
  fetch: typeof fetch;
  baseUrl: string;
  now?: () => number;
}

export function createLocalClient(deps: LocalDeps) {
  const now = deps.now ?? (() => Date.now());
  let renewing: Promise<boolean> | null = null;
  let signedInAt = 0;

  function post(name: string, body: Record<string, string>, keepalive = false): Promise<Response> {
    return deps.fetch(`${deps.baseUrl}${PATH}/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
      keepalive,
    });
  }

  function store(answer: SessionAnswer): void {
    if (!answer.access_token || !answer.refresh_token) throw new LocalAuthError("The API returned no session.");
    setTokens({ accessToken: answer.access_token, refreshToken: answer.refresh_token, expiresAt: now() + (answer.expires_in ?? 300) * 1000 });
  }

  async function send(name: string, body: Record<string, string>): Promise<Response> {
    try {
      return await post(name, body);
    } catch {
      throw new LocalAuthError("Studio could not reach the API.");
    }
  }

  /** The API's words for a refusal (never anything that was typed). */
  async function refusal(response: Response): Promise<LocalAuthError> {
    const answer = (await response.json().catch(() => ({}))) as { detail?: unknown };
    return new LocalAuthError(typeof answer.detail === "string" ? answer.detail : `Studio could not sign you in (${response.status}).`);
  }

  async function authenticate(name: "sign-in" | "password", body: Record<string, string>): Promise<"signed-in" | "change-required"> {
    const response = await send(name, body);
    if (response.status === 404) throw new LocalAuthError("The API has no built-in admin account. Check EAP_IAM_IDENTITY (docs/admin-account.md).");
    if (!response.ok) throw await refusal(response);
    const answer = (await response.json().catch(() => ({}))) as SessionAnswer;
    if (answer.password_change_required) return "change-required";
    store(answer);
    signedInAt = now();
    return "signed-in";
  }

  return {
    /** What to show first: the setup page, the sign-in form, "no method" (the API has no built-in account) or a retry. */
    async status(): Promise<"setup" | "sign-in" | "unavailable" | "unreachable"> {
      try {
        const response = await deps.fetch(`${deps.baseUrl}${PATH}`, { cache: "no-store", credentials: "omit" });
        if (response.status === 404) return "unavailable";
        if (!response.ok) return "unreachable";
        const required = ((await response.json()) as { setup_required?: unknown }).setup_required;
        return typeof required !== "boolean" ? "unreachable" : required ? "setup" : "sign-in";
      } catch {
        return "unreachable";
      }
    },

    /** Create the admin with the one-time setup code; "closed" when there is one already. Signs nobody in. */
    async setUp(setupCode: string, username: string, password: string): Promise<"created" | "closed"> {
      const response = await send("setup", { setup_code: setupCode, username, password });
      if (response.status === 404) return "closed";
      if (!response.ok) throw await refusal(response);
      return "created";
    },

    signIn: (username: string, password: string) => authenticate("sign-in", { username, password }),

    changePassword: (username: string, currentPassword: string, newPassword: string) =>
      authenticate("password", { username, current_password: currentPassword, new_password: newPassword }),

    /** Renew with the rotating refresh token. One at a time; false when it cannot (no session, refused, no network). */
    renew(): Promise<boolean> {
      renewing ??= (async () => {
        const refreshToken = getTokens()?.refreshToken;
        if (!refreshToken) return false;
        try {
          const response = await post("refresh", { refresh_token: refreshToken });
          if (response.status === 401) {
            setTokens(null);
            return false;
          }
          if (!response.ok) return false; // the API is down: the tokens stay, the next 401 or timer tries again
          store((await response.json()) as SessionAnswer);
          return true;
        } catch {
          return false;
        }
      })().finally(() => {
        renewing = null;
      });
      return renewing;
    },

    justLoggedIn(): boolean {
      return signedInAt > 0 && now() - signedInAt < SIGN_IN_LOOP_WINDOW_MS;
    },

    /** Ask for the password again: forgetting the tokens brings the form back. */
    async beginLogin(): Promise<void> {
      setTokens(null);
    },

    /** Forget the tokens, then end the session on the server (best effort: it also expires by itself). */
    async logout(): Promise<void> {
      const refreshToken = getTokens()?.refreshToken;
      signedInAt = 0;
      setTokens(null);
      if (!refreshToken) return;
      await post("sign-out", { refresh_token: refreshToken }, true).catch(() => undefined);
    },
  };
}

export type LocalClient = ReturnType<typeof createLocalClient>;

let client: LocalClient | null = null;

/** The page's client when the deployment has the built-in admin (`EAP_STUDIO_AUTH=local`), else null. */
export function localAuth(): LocalClient | null {
  const runtime = typeof window === "undefined" ? undefined : window.__EAP_RUNTIME_CONFIG__;
  if (runtime?.auth?.mode !== "local" && !runtime?.localSetup) return null;
  client ??= createLocalClient({ fetch: (...args) => window.fetch(...args), baseUrl: BASE_URL });
  return client;
}

export function resetLocalAuthForTests(): void {
  client = null;
}
