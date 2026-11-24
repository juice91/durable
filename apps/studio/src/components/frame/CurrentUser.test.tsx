import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { resetAuthForTests } from "@/lib/auth/controller";
import { resetPictureForTests } from "@/lib/auth/avatar";
import { setTokens } from "@/lib/auth/session";
import { resetCapabilitiesForTests } from "@/lib/iam";

const signOutMock = vi.fn();
vi.mock("@/lib/auth/controller", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/auth/controller")>()), signOut: () => signOutMock() }));

import { CurrentUser } from "./CurrentUser";

const fetchMock = vi.fn();
const ISSUER = "https://auth.example.test";

function answer(me: Record<string, unknown>): Response {
  const body = {
    mode: "dev", identity: [], policy: { name: "p", transitional: true, features: [] }, membership: null, actions: [],
    me: { subject: "s", email: null, tenant_id: "acme", kind: "user", roles: [], provider: "cf-access", ...me },
  };
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

function Wrap({ children }: { children: React.ReactNode }) {
  return <ThemeProvider><TooltipProvider>{children}</TooltipProvider></ThemeProvider>;
}
const renderUser = (rail = false) => render(<CurrentUser rail={rail} />, { wrapper: Wrap });

async function openMenu(rail = false) {
  const user = userEvent.setup();
  const trigger = await screen.findByTestId(rail ? "current-user-avatar" : "current-user-trigger");
  await user.click(trigger);
  await screen.findByTestId("account-menu");
  return { user, trigger };
}

function zitadelSession() {
  window.__EAP_RUNTIME_CONFIG__ = { auth: { mode: "zitadel", issuer: ISSUER, clientId: "c", projectId: "p" } };
  setTokens({ accessToken: "access-secret", expiresAt: Date.now() + 600_000 });
}

beforeEach(() => {
  resetCapabilitiesForTests();
  resetPictureForTests();
  signOutMock.mockReset();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  resetAuthForTests();
  delete window.__EAP_RUNTIME_CONFIG__;
});

describe("CurrentUser and the account menu", () => {
  test("the footer row shows the name and the provider badge; the menu is closed until opened", async () => {
    fetchMock.mockResolvedValue(answer({ email: "joyce@example.com", subject: "sub-1" }));
    renderUser();
    expect(await screen.findByTestId("current-user-name")).toHaveTextContent("joyce@example.com");
    expect(screen.getByTestId("current-user-provider")).toHaveTextContent("Access");
    expect(screen.queryByTestId("account-menu")).toBeNull();
  });

  test("Access: header, roles, tenant, method, Profile, theme and a sign-out link; no account links, no expiry", async () => {
    fetchMock.mockResolvedValue(answer({ email: "joyce@example.com", roles: ["admin", "editor"] }));
    renderUser();
    await openMenu();
    expect(screen.getByTestId("account-name")).toHaveTextContent("joyce@example.com");
    expect(screen.getByTestId("account-provider")).toHaveTextContent("Access");
    expect(screen.getByTestId("account-roles")).toHaveTextContent("admineditor");
    expect(screen.getByTestId("account-tenant")).toHaveTextContent("acme");
    expect(screen.getByTestId("account-method")).toHaveTextContent("Cloudflare Access");
    expect(screen.queryByTestId("account-expiry")).toBeNull();
    expect(screen.getByTestId("account-profile")).toHaveAttribute("href", "/settings?tab=profile");
    expect(screen.queryByTestId("account-link-manage")).toBeNull();
    expect(screen.getByTestId("current-user-signout")).toHaveAttribute("href", "/cdn-cgi/access/logout");
    expect(screen.getByTestId("account-theme-system")).toBeInTheDocument();
  });

  test("ZITADEL: expiry, the three account links to the issuer console in a new tab, and Sign out ends the OIDC session", async () => {
    zitadelSession();
    fetchMock.mockImplementation((url: string) =>
      String(url).includes("/oidc/v1/userinfo") ? Promise.resolve(new Response("{}", { status: 200 })) : Promise.resolve(answer({ provider: "zitadel", email: "zed@example.test", roles: ["viewer"] })),
    );
    renderUser();
    const { user } = await openMenu();
    expect(screen.getByTestId("account-expiry")).toHaveTextContent("Signed in until");
    expect(document.body.textContent).not.toContain("access-secret");
    for (const id of ["manage", "password", "two-factor"]) {
      const link = screen.getByTestId(`account-link-${id}`);
      expect(link).toHaveAttribute("href", `${ISSUER}/ui/console/users/me`);
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    }
    await user.click(screen.getByTestId("current-user-signout"));
    expect(signOutMock).toHaveBeenCalledTimes(1);
  });

  test("loopback dev and API key: no account links and no sign-out", async () => {
    fetchMock.mockResolvedValue(answer({ subject: "operator", provider: "loopback-dev" }));
    renderUser();
    expect(await screen.findByTestId("current-user-name")).toHaveTextContent("operator (loopback dev)");
    await openMenu();
    expect(screen.getByTestId("account-method")).toHaveTextContent("Dev bearer");
    expect(screen.queryByTestId("account-link-manage")).toBeNull();
    expect(screen.queryByTestId("current-user-signout")).toBeNull();
    expect(screen.getByTestId("account-profile")).toBeInTheDocument();
  });

  test("keyboard: Enter opens, Escape closes and focus returns to the trigger", async () => {
    fetchMock.mockResolvedValue(answer({ email: "joyce@example.com" }));
    renderUser();
    const user = userEvent.setup();
    const trigger = await screen.findByTestId("current-user-trigger");
    trigger.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(trigger).toHaveFocus();
    await user.keyboard(" ");
    expect(await screen.findByRole("menu")).toBeInTheDocument();
  });

  test("the theme switch changes the stored preference", async () => {
    fetchMock.mockResolvedValue(answer({ email: "joyce@example.com" }));
    renderUser();
    const { user } = await openMenu();
    await user.click(screen.getByTestId("account-theme-dark"));
    expect(window.localStorage.getItem("studio.theme")).toBe("dark");
  });

  test("the collapsed rail has the same menu behind the avatar button", async () => {
    fetchMock.mockResolvedValue(answer({ email: "joyce@example.com" }));
    renderUser(true);
    const avatar = await screen.findByTestId("current-user-avatar");
    expect(avatar).toHaveTextContent("J");
    expect(avatar).toHaveAttribute("aria-label", "Account menu, joyce@example.com");
    expect(screen.queryByTestId("current-user-name")).toBeNull();
    await openMenu(true);
    expect(screen.getByTestId("account-profile")).toBeInTheDocument();
  });

  test("falls back to an e-mail subject, or a plain subject, when me.email is null", async () => {
    fetchMock.mockResolvedValue(answer({ email: null, subject: "svc-account", provider: "api-key", kind: "api_key" }));
    renderUser();
    expect(await screen.findByTestId("current-user-name")).toHaveTextContent("svc-account");
    expect(screen.getByTestId("current-user-provider")).toHaveTextContent("API key");
  });

  test.each([
    ["a 500", () => Promise.resolve(new Response("boom", { status: 500 }))],
    ["a 401", () => Promise.resolve(new Response("{}", { status: 401 }))],
    ["a network error", () => Promise.reject(new TypeError("offline"))],
  ])("shows nothing and logs nothing on %s", async (_label, respond) => {
    const spies = [vi.spyOn(console, "error"), vi.spyOn(console, "warn"), vi.spyOn(console, "log")].map((s) => s.mockImplementation(() => undefined));
    fetchMock.mockImplementation(respond);
    renderUser();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId("current-user")).toBeNull();
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});
