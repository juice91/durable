import React from "react";
import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ search: "", params: {} as Record<string, string> }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/settings",
  useParams: () => nav.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));

import { ApiKeysSection } from "@/components/apiKeys/ApiKeysSection";
import { ConnectionsSection } from "@/components/settings/ConnectionsSection";
import { SecretsSection } from "@/components/settings/SecretsSection";
import { SettingsSections } from "@/components/settings/SettingsSections";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { resetCapabilitiesForTests } from "@/lib/iam";
import { renderAs, settle, type Routes } from "./roleHarness";
import type { TestRole } from "./iamFixtures";

const NOT_ADMIN: TestRole[] = ["viewer", "editor"];
const T = "2026-11-14T10:00:00Z";
const button = (name: string | RegExp) => screen.queryByRole("button", { name });

vi.unmock("@/lib/permissions");

afterEach(() => {
  cleanup();
  resetCapabilitiesForTests();
  vi.unstubAllGlobals();
  nav.search = "";
});

describe("Settings → Secrets", () => {
  const routes: Routes = [[/GET .*\/secrets/, [{ name: "slack-token", created_at: T, updated_at: T }]]];
  it.each(NOT_ADMIN)("offers a %s no Delete, no Access and no add form", async (role) => {
    renderAs(role, <SecretsSection />, routes);
    await screen.findByText("slack-token");
    await settle();
    expect(button("Delete slack-token")).toBeNull();
    expect(button("Access to slack-token")).toBeNull();
    expect(screen.queryByTestId("secrets-form")).toBeNull();
    expect(screen.getByTestId("why-not").textContent).toContain(`Your role (${role}) can't create a secret`);
  });
  it("offers an admin all of them", async () => {
    renderAs("admin", <SecretsSection />, routes);
    expect(await screen.findByRole("button", { name: "Delete slack-token" })).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Access to slack-token" })).toBeTruthy();
    expect(await screen.findByTestId("secrets-form")).toBeTruthy();
  });
});

describe("Settings → API keys", () => {
  const key = { id: "k1", name: "CI", prefix: "pos_abc", status: "active", scopes: ["agui:run"], agent_ids: null, workflow_ids: null, allowed_origins: null, created_by: "x", created_at: T, expires_at: null, revoked_at: null, last_used_at: null };
  const routes: Routes = [[/GET .*\/api-keys/, { items: [key] }]];
  it.each(NOT_ADMIN)("offers a %s neither Create nor Revoke", async (role) => {
    renderAs(role, <ApiKeysSection />, routes);
    await screen.findByTestId("api-key-row");
    await settle();
    expect(screen.queryByTestId("api-key-create")).toBeNull();
    expect(screen.queryByTestId("api-key-revoke")).toBeNull();
  });
  it("offers an admin both", async () => {
    renderAs("admin", <ApiKeysSection />, routes);
    expect(await screen.findByTestId("api-key-create")).toBeTruthy();
    expect(await screen.findByTestId("api-key-revoke")).toBeTruthy();
  });
});

describe("Settings → Connections", () => {
  const connection = { id: "c1", name: "GitHub", kind: "kb_source", config: { provider: "github" }, base_url: "https://api.github.com", enabled: true, credential_refs: {}, last_status: null, last_error_code: null, last_checked_at: null, created_at: T, updated_at: T };
  const routes: Routes = [[/GET .*\/settings\/connections/, { items: [connection] }], [/GET .*\/kb-sources\/adapters/, { items: [] }]];
  const names = ["Add connection", "Check GitHub", "Edit GitHub", "Disable GitHub", "Delete GitHub"];
  it.each(NOT_ADMIN)("offers a %s no connection controls", async (role) => {
    renderAs(role, <ConnectionsSection />, routes);
    await screen.findByText("GitHub");
    await settle();
    names.forEach((name) => expect(button(name), name).toBeNull());
  });
  it("offers an admin all of them", async () => {
    renderAs("admin", <ConnectionsSection />, routes);
    for (const name of names) expect(await screen.findByRole("button", { name })).toBeTruthy();
  });
});

describe("Settings tabs", () => {
  const page = () => (
    <ThemeProvider>
      <SettingsSections />
    </ThemeProvider>
  );
  it.each(NOT_ADMIN)("does not show a %s the Audit or Members tab, nor their content by address", async (role) => {
    nav.search = "tab=audit";
    renderAs(role, page());
    await settle();
    expect(screen.queryByRole("tab", { name: "Audit" })).toBeNull();
    expect(screen.queryByRole("tab", { name: "Members" })).toBeNull();
    expect(screen.queryByText("The audit log is not available to you.")).toBeNull();
    expect(screen.getByTestId("settings-appearance")).toBeTruthy();
  });
  it("shows an admin both", async () => {
    renderAs("admin", page());
    expect(await screen.findByRole("tab", { name: "Audit" })).toBeTruthy();
    expect(await screen.findByRole("tab", { name: "Members" })).toBeTruthy();
  });
});
