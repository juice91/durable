// A throw-away OIDC provider for the PKCE tests: discovery, an authorize endpoint that checks the S256 challenge and issues
// a one-time code, a token endpoint (authorization_code with PKCE, and rotating refresh tokens), and end_session. It runs
// as a real HTTP server on 127.0.0.1, so the client under test makes real fetches. Test code only.
import { createHash, randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

export interface TestProvider {
  issuer: string;
  /** What the provider saw and did, for assertions. */
  log: { authorizeQuery?: URLSearchParams; tokenForms: URLSearchParams[]; endSession?: URLSearchParams };
  /** Make the next refresh_token grant fail (a revoked session). */
  failRefresh: boolean;
  /** GET the authorize URL the client built, as the browser would after the person signs in: the callback URL, or an error. */
  authorize(url: string): Promise<string>;
  close(): Promise<void>;
}

function b64url(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

export async function startTestProvider(port = 0): Promise<TestProvider> {
  const codes = new Map<string, { challenge: string; nonce: string; redirect: string }>();
  const refreshTokens = new Set<string>();
  let issued = 0;
  const log: TestProvider["log"] = { tokenForms: [] };
  const state = { failRefresh: false };
  let base = "";

  const json = (res: ServerResponse, status: number, body: unknown) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  const readForm = (req: IncomingMessage) =>
    new Promise<URLSearchParams>((resolve) => {
      let data = "";
      req.on("data", (chunk) => (data += chunk));
      req.on("end", () => resolve(new URLSearchParams(data)));
    });

  const tokens = (nonce: string) => {
    issued += 1;
    const refresh = `refresh-${issued}`;
    refreshTokens.add(refresh);
    return {
      access_token: `access-${issued}`,
      refresh_token: refresh,
      id_token: `${b64url({ alg: "none" })}.${b64url({ nonce, sub: "user-1" })}.sig`,
      expires_in: 900,
      token_type: "Bearer",
    };
  };

  const server: Server = createServer(async (req, res) => {
    // A browser calls discovery and the token endpoint cross-origin, as it does ZITADEL.
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Headers", "*");
    if (req.method === "OPTIONS") {
      res.writeHead(204);
      return res.end();
    }
    const url = new URL(req.url ?? "/", base);
    if (url.pathname === "/.well-known/openid-configuration") {
      return json(res, 200, {
        issuer: base,
        authorization_endpoint: `${base}/oauth/v2/authorize`,
        token_endpoint: `${base}/oauth/v2/token`,
        end_session_endpoint: `${base}/oidc/v1/end_session`,
      });
    }
    if (url.pathname === "/oauth/v2/authorize") {
      log.authorizeQuery = url.searchParams;
      const p = url.searchParams;
      if (p.get("response_type") !== "code" || p.get("code_challenge_method") !== "S256" || !p.get("code_challenge")) return json(res, 400, { error: "invalid_request" });
      const code = randomUUID();
      codes.set(code, { challenge: p.get("code_challenge")!, nonce: p.get("nonce") ?? "", redirect: p.get("redirect_uri")! });
      const back = new URL(p.get("redirect_uri")!);
      back.searchParams.set("code", code);
      back.searchParams.set("state", p.get("state") ?? "");
      res.writeHead(302, { Location: back.toString() });
      return res.end();
    }
    if (url.pathname === "/oauth/v2/token" && req.method === "POST") {
      const form = await readForm(req);
      log.tokenForms.push(form);
      if (form.get("grant_type") === "authorization_code") {
        const entry = codes.get(form.get("code") ?? "");
        codes.delete(form.get("code") ?? ""); // a code is single use
        const verifier = form.get("code_verifier") ?? "";
        if (!entry || entry.redirect !== form.get("redirect_uri") || createHash("sha256").update(verifier).digest("base64url") !== entry.challenge) {
          return json(res, 400, { error: "invalid_grant" });
        }
        return json(res, 200, tokens(entry.nonce));
      }
      if (form.get("grant_type") === "refresh_token") {
        const token = form.get("refresh_token") ?? "";
        if (state.failRefresh || !refreshTokens.delete(token)) return json(res, 400, { error: "invalid_grant" }); // rotating: one use
        return json(res, 200, tokens(""));
      }
      return json(res, 400, { error: "unsupported_grant_type" });
    }
    if (url.pathname === "/oidc/v1/end_session") {
      log.endSession = url.searchParams;
      res.writeHead(204);
      return res.end();
    }
    json(res, 404, { error: "not_found" });
  });
  await new Promise<void>((resolve) => server.listen(port, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  return {
    issuer: base,
    log,
    get failRefresh() {
      return state.failRefresh;
    },
    set failRefresh(value: boolean) {
      state.failRefresh = value;
    },
    async authorize(authorizeUrl: string) {
      const response = await fetch(authorizeUrl, { redirect: "manual" });
      const location = response.headers.get("location");
      if (!location) throw new Error(`authorize answered ${response.status}`);
      return location;
    },
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
