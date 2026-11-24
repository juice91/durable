import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { loadPicture, pictureFromIdToken, resetPictureForTests } from "./avatar";
import { resetAuthForTests } from "./controller";
import { setTokens } from "./session";

const ISSUER = "https://auth.example.test";
const fetchMock = vi.fn();

function jwt(claims: Record<string, unknown>): string {
  return `h.${btoa(JSON.stringify(claims))}.s`;
}

beforeEach(() => {
  resetPictureForTests();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  window.fetch = fetchMock as unknown as typeof window.fetch;
  window.__EAP_RUNTIME_CONFIG__ = { auth: { mode: "zitadel", issuer: ISSUER, clientId: "c", projectId: "p" } };
});
afterEach(() => {
  vi.unstubAllGlobals();
  resetAuthForTests();
  delete window.__EAP_RUNTIME_CONFIG__;
});

describe("pictureFromIdToken", () => {
  test("reads an http(s) picture claim and ignores anything else", () => {
    expect(pictureFromIdToken(jwt({ picture: "https://x.test/a.png" }))).toBe("https://x.test/a.png");
    expect(pictureFromIdToken(jwt({ picture: "javascript:alert(1)" }))).toBeNull();
    expect(pictureFromIdToken(jwt({}))).toBeNull();
    expect(pictureFromIdToken("garbage")).toBeNull();
    expect(pictureFromIdToken(undefined)).toBeNull();
  });
});

describe("loadPicture", () => {
  test("a picture on another origin is used as is, with no request", async () => {
    setTokens({ accessToken: "a", idToken: jwt({ picture: "https://cdn.test/me.png" }), expiresAt: Date.now() + 1000 });
    expect(await loadPicture()).toBe("https://cdn.test/me.png");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("without the claim it asks userinfo with the bearer; an issuer-origin image is fetched with the bearer into a blob URL, once per session", async () => {
    setTokens({ accessToken: "tok", expiresAt: Date.now() + 1000 });
    URL.createObjectURL = vi.fn(() => "blob:avatar");
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url.endsWith("/oidc/v1/userinfo")
          ? new Response(JSON.stringify({ picture: `${ISSUER}/assets/v1/me.png` }), { status: 200 })
          : ({ ok: true, blob: async () => ({ type: "image/png" }) } as unknown as Response),
      ),
    );
    expect(await loadPicture()).toBe("blob:avatar");
    expect(await loadPicture()).toBe("blob:avatar");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBe("Bearer tok");
  });

  test("a refused userinfo, a network error or a non-image is no picture", async () => {
    setTokens({ accessToken: "tok", expiresAt: Date.now() + 1000 });
    fetchMock.mockResolvedValue(new Response("no", { status: 403 }));
    expect(await loadPicture()).toBeNull();
    resetPictureForTests();
    fetchMock.mockRejectedValue(new TypeError("cors"));
    expect(await loadPicture()).toBeNull();
  });

  test("outside a ZITADEL session there is nothing to ask", async () => {
    delete window.__EAP_RUNTIME_CONFIG__;
    expect(await loadPicture()).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
