import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ pathname: "/agents", replace: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ replace: nav.replace, push: vi.fn() }) }));

const ctl = vi.hoisted(() => ({
  startLogin: vi.fn(async () => undefined),
  complete: vi.fn(async () => "/workflows"),
  client: null as null | { completeLogin: (s: string) => Promise<string>; justLoggedIn: () => boolean },
}));
vi.mock("@/lib/auth/controller", () => ({
  startLogin: ctl.startLogin,
  oidc: () => ctl.client,
  installAuthFetch: vi.fn(),
  keepFresh: vi.fn(),
  setGiveUpHandler: vi.fn(),
}));

import { AuthGate } from "./AuthGate";
import { AuthFlowError } from "@/lib/auth/oidc";
import { setTokens } from "@/lib/auth/session";

beforeEach(() => {
  setTokens(null);
  nav.pathname = "/agents";
  nav.replace.mockReset();
  ctl.startLogin.mockClear();
  ctl.complete.mockClear();
  ctl.client = { completeLogin: ctl.complete, justLoggedIn: () => false };
});
afterEach(cleanup);

describe("AuthGate", () => {
  it("disabled (access mode, or the loopback page): children untouched, no login", () => {
    render(<AuthGate enabled={false}><p>page</p></AuthGate>);
    expect(screen.getByText("page")).toBeInTheDocument();
    expect(ctl.startLogin).not.toHaveBeenCalled();
  });

  it("enabled with no token: holds the page back and starts the login for the page asked for", async () => {
    render(<AuthGate enabled><p>page</p></AuthGate>);
    expect(screen.queryByText("page")).toBeNull();
    expect(screen.getByTestId("auth-working")).toBeInTheDocument();
    await waitFor(() => expect(ctl.startLogin).toHaveBeenCalledTimes(1));
  });

  it("enabled with a token: shows the page", () => {
    setTokens({ accessToken: "a", expiresAt: Date.now() + 60_000 });
    render(<AuthGate enabled><p>page</p></AuthGate>);
    expect(screen.getByText("page")).toBeInTheDocument();
    expect(ctl.startLogin).not.toHaveBeenCalled();
  });

  it("on the callback it completes the sign-in, shows the page and goes back to where the person was", async () => {
    nav.pathname = "/auth/callback";
    render(<AuthGate enabled><p>page</p></AuthGate>);
    expect(await screen.findByText("page")).toBeInTheDocument();
    expect(ctl.complete).toHaveBeenCalled();
    expect(nav.replace).toHaveBeenCalledWith("/workflows");
  });

  it("a failed callback shows the reason and a way to try again, never a redirect loop", async () => {
    nav.pathname = "/auth/callback";
    ctl.complete.mockRejectedValueOnce(new AuthFlowError("The sign-in response does not match this browser's request. Try again."));
    render(<AuthGate enabled><p>page</p></AuthGate>);
    expect(await screen.findByTestId("auth-error")).toHaveTextContent("does not match");
    expect(screen.queryByText("page")).toBeNull();
    expect(ctl.startLogin).not.toHaveBeenCalled();
  });
});
