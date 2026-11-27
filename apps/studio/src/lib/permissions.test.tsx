import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { capabilitiesFor, iamAnswer, type TestRole } from "@/test/iamFixtures";
import { resetCapabilitiesForTests } from "./iam";
import { Can, usePermissions } from "./permissions";

function stub(handler: (url: string) => Response | Promise<Response>) {
  const fetch = vi.fn(async (url: string) => handler(String(url)));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
const as = (role: TestRole) => stub((url) => iamAnswer(role, url) ?? new Response("{}", { status: 404 }));

function Probe({ id = "a1" }: { id?: string | null }) {
  const p = usePermissions("agent", id, ["run", "edit"]);
  return <p data-testid="probe">{p.loading ? "loading" : `run=${p.can("run")} edit=${p.can("edit")}`}</p>;
}
const probe = () => screen.getByTestId("probe");

vi.unmock("@/lib/permissions");

afterEach(() => {
  cleanup();
  resetCapabilitiesForTests();
  vi.unstubAllGlobals();
});

describe("usePermissions", () => {
  it.each([
    ["viewer", "run=false edit=false"],
    ["editor", "run=true edit=true"],
    ["admin", "run=true edit=true"],
  ] as const)("answers from the server's explanation for a %s", async (role, text) => {
    const fetch = as(role);
    render(<Probe />);
    await waitFor(() => expect(probe()).toHaveTextContent(text));
    expect(fetch.mock.calls.map((c) => String(c[0])).find((u) => u.includes("/iam/explain"))).toContain("kind=agent&id=a1&action=run&action=edit");
  });

  it("offers nothing while the answer is not there", () => {
    stub(() => new Promise<Response>(() => undefined));
    render(
      <>
        <Probe />
        <Can kind="agent" action="create">
          <button>New agent</button>
        </Can>
      </>,
    );
    expect(probe()).toHaveTextContent("loading");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("asks once for the same question", async () => {
    const fetch = as("admin");
    render(
      <>
        <Can kind="agent" action="create"><button>One</button></Can>
        <Can kind="agent" action="create"><button>Two</button></Can>
      </>,
    );
    await screen.findByRole("button", { name: "Two" });
    expect(fetch.mock.calls.filter((c) => String(c[0]).includes("/iam/explain"))).toHaveLength(1);
  });

  it("asks nothing and allows everything when the engine cannot explain (openaccess)", async () => {
    const caps = { ...capabilitiesFor("viewer"), policy: { name: "openaccess", transitional: true, features: [] } };
    const fetch = stub(() => new Response(JSON.stringify(caps), { status: 200 }));
    render(<Probe />);
    await waitFor(() => expect(probe()).toHaveTextContent("run=true edit=true"));
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("allows (the server refuses and explains) when permissions cannot be read", async () => {
    stub(() => new Response("{}", { status: 500 }));
    render(<Probe />);
    await waitFor(() => expect(probe()).toHaveTextContent("run=true edit=true"));
  });

  it("allows when only the explanation cannot be read", async () => {
    stub((url) => (url.includes("/iam/explain") ? new Response("{}", { status: 500 }) : iamAnswer("viewer", url)!));
    render(<Probe />);
    await waitFor(() => expect(probe()).toHaveTextContent("run=true edit=true"));
  });

  it("shows the fallback of Can to a refused role", async () => {
    as("viewer");
    render(
      <Can kind="secret" id="s" action="archive" fallback={<span>read only</span>}>
        <button>Delete</button>
      </Can>,
    );
    expect(await screen.findByText("read only")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
