import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { resetAuthForTests } from "@/lib/auth/controller";
import { resetPictureForTests } from "@/lib/auth/avatar";
import { setTokens } from "@/lib/auth/session";
import { resetCapabilitiesForTests } from "@/lib/iam";
import { ProfileSection } from "./ProfileSection";

const fetchMock = vi.fn();
const ISSUER = "https://auth.example.test";

function respond(me: Record<string, unknown>, audit: () => Response | Promise<Response>) {
  fetchMock.mockImplementation((url: string) => {
    if (String(url).includes("/iam/capabilities")) {
      return Promise.resolve(new Response(JSON.stringify({
        mode: "dev", identity: [], policy: { name: "p", transitional: true, features: [] }, membership: null, actions: [],
        me: { subject: "s", email: "joyce@example.com", tenant_id: "acme", kind: "user", roles: ["admin", "viewer"], provider: "cf-access", ...me },
      }), { status: 200 }));
    }
    if (String(url).includes("/api/v1/audit")) return Promise.resolve(audit());
    return Promise.resolve(new Response("{}", { status: 200 }));
  });
}
const rows = () => new Response(JSON.stringify({ items: [{ id: "1", at: "2026-11-24T10:00:00Z", actor_kind: "user", actor_subject: "s", on_behalf_of: null, action: "create_agent", resource_kind: "agents", resource_id: "a1", outcome: "success", request_id: null, run_id: null, detail: null }], next_cursor: null, retention_days: 400 }), { status: 200 });

beforeEach(() => {
  resetCapabilitiesForTests();
  resetPictureForTests();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  resetAuthForTests();
  delete window.__EAP_RUNTIME_CONFIG__;
});

describe("ProfileSection", () => {
  test("Access: identity, roles with their meaning, the method, my activity via actor=me, no account links, the API keys section", async () => {
    respond({}, rows);
    render(<ProfileSection />);
    expect(await screen.findByTestId("profile-email")).toHaveTextContent("joyce@example.com");
    expect(screen.getByTestId("profile-subject")).toHaveTextContent("s");
    expect(screen.getByTestId("profile-provider")).toHaveTextContent("Access");
    expect(screen.getByTestId("profile-tenant")).toHaveTextContent("acme");
    expect(screen.getByTestId("profile-role-admin")).toHaveTextContent(/Manages the tenant/);
    expect(screen.getByTestId("profile-role-viewer")).toHaveTextContent(/Read-only/);
    expect(screen.getByTestId("profile-method")).toHaveTextContent("Cloudflare Access");
    expect(screen.queryByTestId("profile-expiry")).toBeNull();
    expect(screen.queryByTestId("profile-account")).toBeNull();
    expect(await screen.findByTestId("profile-activity-list")).toHaveTextContent("create_agent");
    const auditCall = fetchMock.mock.calls.map(([u]) => String(u)).find((u) => u.includes("/api/v1/audit"))!;
    expect(new URL(auditCall, "http://x").searchParams.get("actor")).toBe("me");
    expect(await screen.findByTestId("personal-keys-empty")).toBeInTheDocument();
  });

  test("ZITADEL: the session expiry and the account links", async () => {
    window.__EAP_RUNTIME_CONFIG__ = { auth: { mode: "zitadel", issuer: ISSUER, clientId: "c", projectId: "p" } };
    setTokens({ accessToken: "access-secret", expiresAt: Date.now() + 600_000 });
    respond({ provider: "zitadel" }, rows);
    render(<ProfileSection />);
    expect(await screen.findByTestId("profile-expiry")).toHaveTextContent("Studio renews it");
    expect(screen.getByTestId("profile-link-password")).toHaveAttribute("href", `${ISSUER}/ui/console/users/me`);
    expect(document.body.textContent).not.toContain("access-secret");
  });

  test("loopback: no account section", async () => {
    respond({ provider: "loopback-dev", subject: "operator", email: null }, rows);
    render(<ProfileSection />);
    expect(await screen.findByTestId("profile-method")).toHaveTextContent("Dev bearer");
    expect(screen.queryByTestId("profile-account")).toBeNull();
    expect(screen.getByTestId("profile-api-keys-unavailable")).toBeInTheDocument();
  });

  test("an empty or failing activity read says so and does not break the page", async () => {
    respond({}, () => new Response(JSON.stringify({ items: [], next_cursor: null, retention_days: 400 }), { status: 200 }));
    render(<ProfileSection />);
    expect(await screen.findByTestId("profile-activity-empty")).toBeInTheDocument();
    cleanup();
    resetCapabilitiesForTests();
    respond({}, () => new Response("boom", { status: 500 }));
    render(<ProfileSection />);
    await waitFor(() => expect(screen.getByText("Your activity could not be loaded.")).toBeInTheDocument());
    expect(screen.getByTestId("profile-identity")).toBeInTheDocument();
  });
});
