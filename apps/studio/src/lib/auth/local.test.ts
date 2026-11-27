import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authorizationValue } from "../credentials";
import { installAuthFetch, resetAuthForTests, sessionClient, setGiveUpHandler, signOut } from "./controller";
import { localAuth, LocalAuthError } from "./local";
import { getTokens, setTokens } from "./session";

// Obviously fake values: no real password or token.
const PASSWORD = "fake-password-0001";
const session = (n: number) => ({
  password_change_required: false, access_token: `access-${n}`, refresh_token: `refresh-${n}`, token_type: "Bearer", expires_in: 900,
});
const json = (status: number, body: unknown) => new Response(status === 204 ? null : JSON.stringify(body), { status });

let calls: { url: string; body: Record<string, string> | undefined; authorization: string | null }[] = [];
let answer: (url: string, body?: Record<string, string>, authorization?: string | null) => Response | Promise<Response>;

beforeEach(() => {
  resetAuthForTests();
  calls = [];
  window.__EAP_RUNTIME_CONFIG__ = { auth: { mode: "local" } };
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "");
  window.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as Record<string, string>) : undefined;
    const authorization = new Headers(init?.headers).get("Authorization");
    calls.push({ url, body, authorization });
    return answer(url, body, authorization);
  }) as typeof window.fetch;
});
afterEach(() => {
  vi.unstubAllEnvs();
  resetAuthForTests();
  delete window.__EAP_RUNTIME_CONFIG__;
});

describe("the built-in admin session", () => {
  it("signs in and keeps the tokens in memory only", async () => {
    answer = () => json(200, session(1));
    await expect(localAuth()!.signIn("admin", PASSWORD)).resolves.toBe("signed-in");
    expect(calls[0].url).toMatch(/\/api\/v1\/auth\/local\/sign-in$/);
    expect(calls[0].body).toEqual({ username: "admin", password: PASSWORD });
    expect(authorizationValue()).toBe("Bearer access-1");
    expect(JSON.stringify([{ ...localStorage }, { ...sessionStorage }, document.cookie])).not.toMatch(/access-1|refresh-1|fake-password/);
  });

  it("an initial password gets no token: the change comes first", async () => {
    answer = () => json(200, { password_change_required: true });
    await expect(localAuth()!.signIn("admin", PASSWORD)).resolves.toBe("change-required");
    expect(getTokens()).toBeNull();
    answer = () => json(200, session(2));
    await localAuth()!.changePassword("admin", PASSWORD, "fake-password-0002");
    expect(calls[1].body).toEqual({ username: "admin", current_password: PASSWORD, new_password: "fake-password-0002" });
    expect(getTokens()?.accessToken).toBe("access-2");
  });

  it.each([
    [401, { detail: "Invalid username or password", code: "invalid_credentials" }, "Invalid username or password"],
    [429, { detail: "Too many sign-in attempts. Wait 15 minutes, then try again.", code: "locked" }, "Too many sign-in attempts. Wait 15 minutes, then try again."],
    [422, { detail: [{ loc: ["body"], msg: "x", type: "y" }] }, "Studio could not sign you in (422)."],
    [404, { detail: "Not Found" }, "The API has no built-in admin account. Check EAP_IAM_IDENTITY (docs/admin-account.md)."],
  ])("a %s answer is told in words and keeps no token", async (status, body, words) => {
    answer = () => json(status as number, body);
    await expect(localAuth()!.signIn("admin", PASSWORD)).rejects.toThrow(new LocalAuthError(words as string));
    expect(getTokens()).toBeNull();
  });

  it("an unreachable API is told without the password", async () => {
    answer = () => Promise.reject(new TypeError("network"));
    await expect(localAuth()!.signIn("admin", PASSWORD)).rejects.toThrow("Studio could not reach the API.");
  });

  it("a 401 renews once with the rotating refresh token and repeats the call", async () => {
    setTokens({ accessToken: "access-1", refreshToken: "refresh-1", expiresAt: Date.now() + 900_000 });
    answer = (url, _body, authorization) =>
      url.endsWith("/refresh") ? json(200, session(2)) : authorization === "Bearer access-2" ? json(200, []) : json(401, {});
    installAuthFetch();
    const response = await window.fetch("/api/v1/agents", { headers: { Authorization: "Bearer access-1" } });
    expect(response.status).toBe(200);
    expect(calls.map((c) => c.url.replace(/.*\/api\/v1/, ""))).toEqual(["/agents", "/auth/local/refresh", "/agents"]);
    expect(calls[1].body).toEqual({ refresh_token: "refresh-1" });
    expect(getTokens()?.refreshToken).toBe("refresh-2");
  });

  it("renews once at a time", async () => {
    setTokens({ accessToken: "access-1", refreshToken: "refresh-1", expiresAt: Date.now() });
    answer = () => json(200, session(2));
    await Promise.all([sessionClient()!.renew(), sessionClient()!.renew()]);
    expect(calls).toHaveLength(1);
  });

  it("a refused renew forgets the session (the sign-in form comes back)", async () => {
    setTokens({ accessToken: "access-1", refreshToken: "refresh-1", expiresAt: Date.now() + 900_000 });
    answer = () => json(401, { detail: "x", code: "invalid_token" });
    installAuthFetch();
    await window.fetch("/api/v1/agents");
    expect(getTokens()).toBeNull();
  });

  it("a network failure during a renew keeps the tokens", async () => {
    setTokens({ accessToken: "access-1", refreshToken: "refresh-1", expiresAt: Date.now() });
    answer = () => Promise.reject(new TypeError("network"));
    await expect(sessionClient()!.renew()).resolves.toBe(false);
    expect(getTokens()?.refreshToken).toBe("refresh-1");
  });

  it("a 401 right after signing in stops with a message instead of asking again", async () => {
    answer = (url) => (url.includes("/auth/local/") ? json(200, session(1)) : json(401, {}));
    await localAuth()!.signIn("admin", PASSWORD);
    const gaveUp = vi.fn();
    setGiveUpHandler(gaveUp);
    installAuthFetch();
    await window.fetch("/api/v1/agents");
    expect(gaveUp).toHaveBeenCalled();
  });

  it("a wrong password during a session does not trigger a renew", async () => {
    setTokens({ accessToken: "access-1", refreshToken: "refresh-1", expiresAt: Date.now() + 900_000 });
    answer = () => json(401, { detail: "Invalid username or password", code: "invalid_credentials" });
    installAuthFetch();
    await window.fetch("/api/v1/auth/local/password", { method: "POST", body: "{}" });
    expect(calls).toHaveLength(1);
    expect(getTokens()).not.toBeNull();
  });

  it("sign-out forgets the tokens and ends the session on the server, even when that fails", async () => {
    setTokens({ accessToken: "access-1", refreshToken: "refresh-1", expiresAt: Date.now() + 900_000 });
    answer = () => Promise.reject(new TypeError("network"));
    await signOut();
    expect(getTokens()).toBeNull();
    expect(calls[0].url).toMatch(/\/auth\/local\/sign-out$/);
    expect(calls[0].body).toEqual({ refresh_token: "refresh-1" });
  });

  it.each([
    [200, { setup_required: true }, "setup"],
    [200, { setup_required: false }, "sign-in"],
    [404, { detail: "Not Found" }, "unavailable"],
    [503, {}, "unreachable"],
    [200, { unexpected: 1 }, "unreachable"],
  ])("asks what to show first (%s %o)", async (status, body, expected) => {
    answer = () => json(status as number, body);
    await expect(localAuth()!.status()).resolves.toBe(expected);
    expect(calls[0].url).toMatch(/\/api\/v1\/auth\/local$/);
  });

  it("an unreachable API is a status, not an error", async () => {
    answer = () => Promise.reject(new TypeError("network"));
    await expect(localAuth()!.status()).resolves.toBe("unreachable");
  });

  it("creates the admin with the setup code and signs nobody in", async () => {
    answer = () => json(201, { username: "owner" });
    await expect(localAuth()!.setUp("FAKE-CODE", "owner", PASSWORD)).resolves.toBe("created");
    expect(calls[0].url).toMatch(/\/auth\/local\/setup$/);
    expect(calls[0].body).toEqual({ setup_code: "FAKE-CODE", username: "owner", password: PASSWORD });
    expect(getTokens()).toBeNull();
  });

  it("says when setup is already closed, and tells a refusal in words", async () => {
    answer = () => json(404, { detail: "Not Found" });
    await expect(localAuth()!.setUp("FAKE-CODE", "owner", PASSWORD)).resolves.toBe("closed");
    answer = () => json(401, { detail: "That setup code is not right.", code: "invalid_setup_code" });
    await expect(localAuth()!.setUp("FAKE-CODE", "owner", PASSWORD)).rejects.toThrow("That setup code is not right.");
  });

  it("does nothing outside local mode", () => {
    window.__EAP_RUNTIME_CONFIG__ = {};
    expect(localAuth()).toBeNull();
    expect(sessionClient()).toBeNull();
  });

  it("the loopback page keeps the dev bearer", () => {
    const bearer = ["Bearer", "dev:studio:operator"].join(" ");
    window.__EAP_RUNTIME_CONFIG__ = { devBearer: bearer };
    expect(window.location.hostname).toBe("localhost");
    expect(authorizationValue()).toBe(bearer);
    expect(sessionClient()).toBeNull();
  });
});
