import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ search: "" }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/settings",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
  redirect: vi.fn(),
}));
vi.mock("./MembersSection", () => ({ MembersSection: () => <section data-testid="members-stub" /> }));

import SettingsPage from "@/app/settings/page";
import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { resetCapabilitiesForTests } from "@/lib/iam";

let media: MatchMediaControl;
const fetchMock = vi.fn();
const caps = (membership: unknown, actions: string[]) => () => new Response(JSON.stringify({
  mode: "production", identity: [], policy: { name: "p", transitional: false, features: [] }, membership,
  me: { subject: "a@b.co", email: "a@b.co", tenant_id: "acme", kind: "user", roles: [], provider: "zitadel" }, actions,
}), { status: 200, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  media = mockMatchMedia(true);
  resetCapabilitiesForTests();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  nav.search = "";
});
afterEach(() => {
  cleanup();
  media.restore();
  vi.unstubAllGlobals();
});

const page = () => render(<FrameHarness><SettingsPage /></FrameHarness>);

describe("the Members tab follows the capabilities", () => {
  it("is shown when a membership source exists and the caller may manage members", async () => {
    fetchMock.mockImplementation(caps({ name: "zitadel", mutable: true }, ["tenant.manage_members"]));
    page();
    expect(await screen.findByRole("tab", { name: "Members" })).toBeInTheDocument();
  });

  it("is hidden with no membership plug-in", async () => {
    fetchMock.mockImplementation(caps(null, ["tenant.manage_members"]));
    page();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("tab", { name: "Members" })).toBeNull();
  });

  it("is hidden from a caller who may not manage members, and ?tab=members falls back", async () => {
    nav.search = "tab=members";
    fetchMock.mockImplementation(caps({ name: "zitadel", mutable: true }, []));
    page();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("tab", { name: "Members" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Appearance" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("members-stub")).toBeNull();
  });
});
