import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ pathname: "/agents", replace: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ replace: nav.replace, push: vi.fn() }) }));

import { AuthGate } from "./AuthGate";
import { NoSignInMethod } from "./NoSignInMethod";
import { resetAuthForTests, signOut } from "@/lib/auth/controller";
import { getTokens, setTokens } from "@/lib/auth/session";
import { AXE_TIMEOUT_MS, expectNoAxeViolations, PAGE_RULES } from "@/test/a11y";

const PASSWORD = "fake-password-0001";
const NEXT = "fake-password-0002";
const json = (status: number, body: unknown) => new Response(status === 204 ? null : JSON.stringify(body), { status });
const session = { password_change_required: false, access_token: "access-1", refresh_token: "refresh-1", expires_in: 900 };
let answers: Response[] = [];
let sent: Record<string, string>[] = [];

beforeEach(() => {
  resetAuthForTests();
  nav.pathname = "/agents";
  nav.replace.mockReset();
  window.history.replaceState(null, "", "/");
  answers = [];
  sent = [];
  window.__EAP_RUNTIME_CONFIG__ = { auth: { mode: "local" } };
  window.fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    sent.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, string>);
    return answers.shift() ?? json(500, {});
  }) as typeof window.fetch;
});
afterEach(() => {
  cleanup();
  resetAuthForTests();
  delete window.__EAP_RUNTIME_CONFIG__;
});

/** The gate asks the API what to show; `required` is its answer, queued before the others. */
async function gate(required = false) {
  answers.unshift(json(200, { setup_required: required }));
  const view = render(<AuthGate enabled={false} local><p>the page</p></AuthGate>);
  await screen.findByRole("heading", { level: 1 });
  sent.length = 0;
  return view;
}
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const submit = () => fireEvent.submit(screen.getByTestId("local-sign-in-form"));

describe("the built-in admin sign-in", () => {
  it("holds the page back and shows a form", async () => {
    await gate();
    expect(screen.queryByText("the page")).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "current-password");
  });

  it("signs in and shows the page", async () => {
    answers = [json(200, session)];
    await gate();
    type("Username", "admin");
    type("Password", PASSWORD);
    submit();
    expect(await screen.findByText("the page")).toBeInTheDocument();
    expect(sent[0]).toEqual({ username: "admin", password: PASSWORD });
  });

  it("shows the API's refusal, clears the password and stays", async () => {
    answers = [json(401, { detail: "Invalid username or password" })];
    await gate();
    type("Username", "admin");
    type("Password", PASSWORD);
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(screen.queryByText("the page")).toBeNull();
  });

  it("forces the change of an initial password, checking the two entries first", async () => {
    answers = [json(200, { password_change_required: true }), json(422, { detail: "The password must not be the username.", code: "weak_password" }), json(200, session)];
    await gate();
    type("Username", "admin");
    type("Password", PASSWORD);
    submit();
    expect(await screen.findByRole("heading", { level: 1, name: "Choose a new password" })).toBeInTheDocument();
    expect(screen.getByLabelText("New password")).toHaveAttribute("autocomplete", "new-password");

    type("New password", "short");
    type("Repeat the new password", "short");
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent("at least 12 characters");
    type("New password", NEXT);
    type("Repeat the new password", NEXT + "x");
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent("do not match");
    expect(sent).toHaveLength(1);

    type("Repeat the new password", NEXT);
    submit();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("must not be the username"));
    await waitFor(() => expect(screen.getByTestId("local-sign-in-submit")).toBeEnabled());
    submit();
    expect(await screen.findByText("the page")).toBeInTheDocument();
    expect(sent[2]).toEqual({ username: "admin", current_password: PASSWORD, new_password: NEXT });
  });

  it("returns to the form after sign-out", async () => {
    setTokens({ accessToken: "a", refreshToken: "r", expiresAt: Date.now() + 900_000 });
    answers = [json(204, null), json(200, { setup_required: false })];
    render(<AuthGate enabled={false} local><p>the page</p></AuthGate>);
    expect(screen.getByText("the page")).toBeInTheDocument();
    await signOut();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(getTokens()).toBeNull();
  });

  it("has no accessibility violations on either step", async () => {
    answers = [json(200, { password_change_required: true })];
    await gate();
    await expectNoAxeViolations(document.body, { rules: PAGE_RULES });
    type("Username", "admin");
    type("Password", PASSWORD);
    submit();
    await screen.findByLabelText("New password");
    await expectNoAxeViolations(document.body, { rules: PAGE_RULES });
  }, AXE_TIMEOUT_MS);
});

describe("first-run setup", () => {
  const fill = (code: string, user: string, password: string, again = password) => {
    type("Setup code", code);
    type("Username", user);
    type("Password", password);
    type("Repeat the password", again);
    fireEvent.submit(screen.getByTestId("setup-form"));
  };

  it("offers the setup page while there is no admin, saying where the code is", async () => {
    await gate(true);
    expect(screen.getByRole("heading", { level: 1, name: "Set up the admin account" })).toBeInTheDocument();
    expect(screen.getByTestId("setup-form")).toHaveTextContent("setup code");
    expect(screen.getByText("Where do I find the setup code?")).toBeInTheDocument();
    expect(screen.getByTestId("setup-form")).toHaveTextContent("kubectl -n pydanticos exec deploy/api -- python -m eap_iam_localadmin setup-code");
    expect(screen.getByTestId("setup-form")).toHaveTextContent("docker compose exec api setup-link");
    expect(screen.getByTestId("setup-form")).toHaveTextContent(/at least 12 characters/i);
    expect(screen.getByLabelText("Setup code")).toHaveAttribute("autocomplete", "off");
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "new-password");
    expect(screen.queryByText("the page")).toBeNull();
  });

  it("checks the entries before asking the API", async () => {
    await gate(true);
    fill("FAKE-CODE", "owner", "short");
    expect(screen.getByRole("alert")).toHaveTextContent("at least 12 characters");
    fill("FAKE-CODE", "owner", PASSWORD, NEXT);
    expect(screen.getByRole("alert")).toHaveTextContent("do not match");
    fill("FAKE-CODE", "administrator", "Administrator");
    expect(screen.getByRole("alert")).toHaveTextContent("must not be the username");
    expect(sent).toHaveLength(0);
  });

  it("creates the admin and signs straight in", async () => {
    await gate(true);
    answers = [json(401, { detail: "That setup code is not right." }), json(201, { username: "owner" }), json(200, session)];
    fill("WRONG", "owner", PASSWORD);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("not right"));
    await waitFor(() => expect(screen.getByTestId("setup-submit")).toBeEnabled());
    fill("FAKE-CODE", "owner", PASSWORD);
    expect(await screen.findByText("the page")).toBeInTheDocument();
    expect(sent[1]).toEqual({ setup_code: "FAKE-CODE", username: "owner", password: PASSWORD });
    expect(sent[2]).toEqual({ username: "owner", password: PASSWORD });
  });

  it("falls back to the sign-in form when the automatic sign-in fails", async () => {
    await gate(true);
    answers = [json(201, { username: "owner" }), json(503, {})];
    fill("FAKE-CODE", "owner", PASSWORD);
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("created");
    expect(getTokens()).toBeNull();
  });

  it("fills in the code from the link's fragment and takes it out of the address bar", async () => {
    window.history.replaceState(null, "", "/setup#code=FAKE-CODE-FROM-LINK");
    await gate(true);
    expect(screen.getByLabelText("Setup code")).toHaveValue("FAKE-CODE-FROM-LINK");
    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe("/setup");
    expect(screen.getByLabelText("Username")).toHaveFocus();
  });

  it("shows the password rules as they are met", async () => {
    await gate(true);
    const rule = (name: RegExp) => screen.getByText(name).closest("li")!;
    expect(rule(/at least 12 characters/i)).toHaveAttribute("data-met", "false");
    type("Username", "owner");
    type("Password", PASSWORD);
    expect(rule(/at least 12 characters/i)).toHaveAttribute("data-met", "true");
    expect(rule(/not your username/i)).toHaveAttribute("data-met", "true");
    expect(rule(/both entries match/i)).toHaveAttribute("data-met", "false");
    type("Repeat the password", PASSWORD);
    expect(rule(/both entries match/i)).toHaveAttribute("data-met", "true");
  });

  it("on the loopback page (dev bearer) /setup still creates the account, signs nobody in and goes home", async () => {
    window.__EAP_RUNTIME_CONFIG__ = { localSetup: true, devBearer: ["Bearer", "dev:studio:operator"].join(" ") };
    nav.pathname = "/setup";
    answers = [json(200, { setup_required: true }), json(201, { username: "owner" })];
    render(<AuthGate enabled={false} localSetup><p>the page</p></AuthGate>);
    await screen.findByRole("heading", { name: "Set up the admin account" });
    fill("FAKE-CODE", "owner", PASSWORD);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/agents"));
    expect(sent).toHaveLength(2); // the status and the setup: no sign-in
    expect(getTokens()).toBeNull();
  });

  it("on that page every other path is untouched, and /setup goes home once there is an admin", async () => {
    window.__EAP_RUNTIME_CONFIG__ = { localSetup: true };
    const first = render(<AuthGate enabled={false} localSetup><p>the page</p></AuthGate>);
    expect(screen.getByText("the page")).toBeInTheDocument();
    expect(sent).toHaveLength(0);
    first.unmount();
    nav.pathname = "/setup";
    answers = [json(200, { setup_required: false })];
    render(<AuthGate enabled={false} localSetup><p>the page</p></AuthGate>);
    expect(await screen.findByText("the page")).toBeInTheDocument();
    expect(nav.replace).toHaveBeenCalledWith("/agents");
  });

  it("goes to the sign-in form when someone else finished the setup", async () => {
    await gate(true);
    answers = [json(404, { detail: "Not Found" })];
    fill("FAKE-CODE", "owner", PASSWORD);
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
  });

  it("says there is no sign-in method when the API has no built-in admin account", async () => {
    answers = [json(404, { detail: "Not Found" })];
    render(<AuthGate enabled={false} local><p>the page</p></AuthGate>);
    expect(await screen.findByRole("heading", { name: "No sign-in method is configured" })).toBeInTheDocument();
  });

  it("offers to try again while the API cannot be reached", async () => {
    answers = [json(503, {}), json(200, { setup_required: false })];
    render(<AuthGate enabled={false} local><p>the page</p></AuthGate>);
    expect(await screen.findByRole("heading", { name: "Studio cannot reach the API" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    await gate(true);
    await expectNoAxeViolations(document.body, { rules: PAGE_RULES });
  }, AXE_TIMEOUT_MS);
});

describe("no sign-in method", () => {
  it("says so, with where to look, and has no accessibility violations", async () => {
    render(<NoSignInMethod />);
    expect(screen.getByRole("heading", { level: 1, name: "No sign-in method is configured" })).toBeInTheDocument();
    expect(screen.getByTestId("no-sign-in")).toHaveTextContent("EAP_STUDIO_AUTH");
    await expectNoAxeViolations(document.body, { rules: PAGE_RULES });
  }, AXE_TIMEOUT_MS);
});
