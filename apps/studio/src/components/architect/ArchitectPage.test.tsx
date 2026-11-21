import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";

vi.mock("next/navigation", () => ({
  usePathname: () => "/architect",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/components/agentPage/ChatTab", () => ({ ChatTab: ({ agentId }: { agentId: string }) => <div data-testid="chat-tab" data-agent={agentId} /> }));

import { FrameHarness } from "@/components/frame/frameTesting";
import ArchitectPageRoute from "@/app/architect/page";
import { DRAFT_RESPONSE, OPTIONS, SEEDED_STATE, stubArchitectApi } from "./architectFixtures";
import { chooseValue, optionsOf } from "@/test/select";

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function mount(routes: Parameters<typeof stubArchitectApi>[0]) {
  const api = stubArchitectApi({ "GET /architect/options": OPTIONS, ...routes });
  vi.stubGlobal("fetch", vi.fn(api.fetchMock));
  render(<FrameHarness><ArchitectPageRoute /></FrameHarness>);
  return api;
}

/** A small stand-in for user-event: Radix tabs open on mouse down, so click is a full pointer sequence. */
const user = {
  click: async (el: Element) => {
    await act(async () => {
      fireEvent.pointerDown(el, { button: 0, ctrlKey: false });
      fireEvent.mouseDown(el, { button: 0, ctrlKey: false });
      fireEvent.pointerUp(el);
      fireEvent.mouseUp(el);
      fireEvent.click(el);
    });
  },
  type: async (el: Element, text: string) => {
    await act(async () => {
      fireEvent.change(el, { target: { value: text } });
    });
  },
};

async function fillFromDescription() {
  await user.type(await screen.findByLabelText("Describe it"), "An agent that answers refund questions");
  await user.click(screen.getByRole("button", { name: "Fill in the form" }));
  await screen.findByTestId("architect-assumptions");
}

describe("Architect page", () => {
  test("a seeded tenant lists created agents and workflows by name with id · vN beneath, archived behind a toggle", async () => {
    window.localStorage.clear();
    const api = mount({ "GET /architect": SEEDED_STATE });
    const list = await screen.findByTestId("architect-created");
    expect(within(list).getByText("FAQ bot")).toBeInTheDocument();
    expect(within(list).getByText("faq-bot · v2")).toBeInTheDocument();
    expect(within(list).getByText("Triage")).toBeInTheDocument();
    expect(within(list).getByText("triage · v1")).toBeInTheDocument();
    expect(within(list).getAllByTestId("architect-draft-marker")).toHaveLength(1);
    expect(within(list).queryByText("Old bot")).not.toBeInTheDocument();
    expect(within(list).queryByText("Archived")).not.toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "Show archived (1)" }));
    expect(within(list).getByText("Old bot")).toBeInTheDocument();
    expect(within(list).getByText("Archived")).toBeInTheDocument();
    expect(window.localStorage.getItem("studio.architect.show-archived")).toBe("true");
    expect(api.calls.some((c) => c.key === "POST /architect")).toBe(false);
  });

  test("seeds on first load when the tenant has no Architect yet", async () => {
    let seeded = false;
    const api = mount({
      "GET /architect": () => ({ body: { ...SEEDED_STATE, seeded, agents: [], workflows: [] } }),
      "POST /architect": () => {
        seeded = true;
        return { status: 201, body: { architect_agent_id: "row-arch", drafter_agent_id: "row-draft", model: "litellm:qwen3.8" } };
      },
    });
    await waitFor(() => expect(api.calls.filter((c) => c.key === "POST /architect")).toHaveLength(1));
    expect(await screen.findByRole("button", { name: "Fill in the form" })).toBeInTheDocument();
  });

  test("shows the choose-model state when the seed has no model to use", async () => {
    mount({
      "GET /architect": { ...SEEDED_STATE, seeded: false, choose_model: true, agents: [], workflows: [] },
      "POST /architect": () => ({ status: 409, body: { detail: "Choose a model for the Architect first: no model is available.", code: "choose_model", providers: [] } }),
    });
    const state = await screen.findByTestId("architect-choose-model");
    expect(state).toHaveTextContent("Choose a model");
    expect(within(state).getByRole("link", { name: "Open providers" })).toHaveAttribute("href", "/settings?tab=providers");
    expect(screen.queryByRole("button", { name: "Fill in the form" })).toBeNull();
  });

  test("Fill in the form posts the description and fills the fields", async () => {
    const api = mount({ "GET /architect": SEEDED_STATE, "POST /architect/draft": DRAFT_RESPONSE });
    await fillFromDescription();
    expect(api.calls.find((c) => c.key === "POST /architect/draft")?.body).toMatchObject({ description: "An agent that answers refund questions" });
    expect(screen.getByLabelText("Name")).toHaveValue("Refund helper");
    expect(screen.getByLabelText("Id")).toHaveValue("refund-helper");
    expect(screen.getByLabelText("Description")).toHaveValue("Answers refund questions.");
    expect(screen.getByLabelText("Instructions")).toHaveValue("Help the customer with refunds.");
    expect(screen.getByLabelText("Model")).toHaveValue("litellm:other");
    expect(screen.getByLabelText("Memory")).toBeChecked();
  });

  test("the assumptions are listed for review and the dropped items are noted", async () => {
    mount({ "GET /architect": SEEDED_STATE, "POST /architect/draft": DRAFT_RESPONSE });
    await fillFromDescription();
    const assumptions = screen.getByTestId("architect-assumptions");
    expect(within(assumptions).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Refunds are allowed within 30 days.", "Replies are in English."]);
    expect(screen.getByTestId("architect-dropped")).toHaveTextContent("shell.run");
  });

  test("granted tools are checkboxes that can be cleared; a tool that needs approval is marked", async () => {
    const api = mount({ "GET /architect": SEEDED_STATE, "POST /architect/draft": DRAFT_RESPONSE, "POST /architect/agents": { id: "refund-helper", agent_id: "row-9", version: 1, published: false, warnings: [] } });
    await fillFromDescription();
    const email = screen.getByRole("checkbox", { name: /Send email/ });
    expect(email).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /HTTP GET/ })).toBeChecked();
    expect(screen.getByTestId("architect-needs-approval")).toHaveTextContent("needs approval");
    await user.click(email);
    expect(email).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.calls.some((c) => c.key === "POST /architect/agents")).toBe(true));
    expect((api.calls.find((c) => c.key === "POST /architect/agents")?.body as { spec: { tools: string[] } }).spec.tools).toEqual(["http.get"]);
  });

  test("the model picker only offers the option models", async () => {
    mount({ "GET /architect": SEEDED_STATE });
    const model = await screen.findByLabelText("Model");
    const values = optionsOf(model).map((o) => (o as HTMLOptionElement).value).filter(Boolean);
    expect(values).toEqual(["litellm:qwen3.8", "litellm:other"]);
  });

  test("Save draft posts publish false and does not claim to be live", async () => {
    const api = mount({ "GET /architect": SEEDED_STATE, "POST /architect/draft": DRAFT_RESPONSE, "POST /architect/agents": { id: "refund-helper", agent_id: "row-9", version: 1, published: false, warnings: [] } });
    await fillFromDescription();
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await screen.findByText(/Saved as a draft/);
    expect(api.calls.find((c) => c.key === "POST /architect/agents")?.body).toMatchObject({ publish: false });
  });

  test("Save and go live posts publish true, shows the publish warnings and sends the version it was saved at", async () => {
    const bodies: unknown[] = [];
    mount({
      "GET /architect": SEEDED_STATE,
      "POST /architect/draft": DRAFT_RESPONSE,
      "POST /architect/agents": (body: unknown) => {
        bodies.push(body);
        return { body: { id: "refund-helper", agent_id: "row-9", version: bodies.length, published: true, warnings: [{ code: "model_unpriced", path: "model", message: "This model has no price, so the cost cap cannot apply." }] } };
      },
    });
    await fillFromDescription();
    await user.click(screen.getByLabelText(/I have reviewed these assumptions/));
    await user.click(screen.getByRole("button", { name: "Save and go live" }));
    expect(await screen.findByTestId("publish-warnings")).toHaveTextContent("no price");
    expect(bodies[0]).toMatchObject({ publish: true });
    await user.click(screen.getByRole("button", { name: "Save and go live" }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toMatchObject({ publish: true, expected_version: 1 });
  });

  test("Save and go live waits for the assumptions to be reviewed", async () => {
    mount({ "GET /architect": SEEDED_STATE, "POST /architect/draft": DRAFT_RESPONSE });
    await fillFromDescription();
    expect(screen.getByRole("button", { name: "Save and go live" })).toBeDisabled();
    await user.click(screen.getByLabelText(/I have reviewed these assumptions/));
    expect(screen.getByRole("button", { name: "Save and go live" })).toBeEnabled();
  });

  test("New offers an agent or a workflow; Workflow opens a fresh Workflow tab, Agent returns to the form", async () => {
    mount({ "GET /architect": SEEDED_STATE });
    const pick = async (item: string) => {
      await act(async () => {
        fireEvent.keyDown(screen.getByTestId("architect-new"), { key: "Enter" });
      });
      const menu = await screen.findByRole("menu");
      await act(async () => {
        fireEvent.click(within(menu).getByRole("menuitem", { name: item }));
      });
    };
    await screen.findByTestId("architect-new");
    await pick("Workflow");
    await waitFor(() => expect(screen.getByRole("tab", { name: "Workflow" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByRole("tab", { name: "Form" })).toHaveAttribute("aria-selected", "false");
    await pick("Agent");
    await waitFor(() => expect(screen.getByRole("tab", { name: "Form" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByLabelText("Name")).toHaveValue("");
  });

  test("a stale version (409) is readable and offers to reload", async () => {
    mount({
      "GET /architect": SEEDED_STATE,
      "GET /agents/row-1": { id: "row-1", name: "FAQ bot", latest_version: 3, draft_spec: { spec_version: 1, name: "FAQ bot", instructions: "Answer FAQs.", model: { provider: "litellm", name: "qwen3.8" }, tools: [] } },
      "POST /architect/agents": () => ({ status: 409, body: { detail: "version mismatch", code: "version_mismatch", latest_version: 3 } }),
    });
    await user.click(await screen.findByRole("button", { name: /FAQ bot/ }));
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("FAQ bot"));
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    const alert = await screen.findByTestId("architect-version-mismatch");
    expect(alert).toHaveTextContent("changed since you opened it");
    expect(alert).toHaveTextContent("v3");
    expect(within(alert).getByRole("button", { name: "Reload the latest" })).toBeInTheDocument();
  });

  test("a refused save shows the API's reason", async () => {
    mount({
      "GET /architect": SEEDED_STATE,
      "POST /architect/draft": DRAFT_RESPONSE,
      "POST /architect/agents": () => ({ status: 422, body: { detail: "The tool send_email is not allowed.", code: "tool_not_allowed", tools: ["send_email"] } }),
    });
    await fillFromDescription();
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The tool send_email is not allowed.");
  });

  test("a draft that fails shows the reason and keeps the description", async () => {
    mount({ "GET /architect": SEEDED_STATE, "POST /architect/draft": () => ({ status: 504, body: { detail: "The drafter took too long.", code: "draft_timeout" } }) });
    await user.type(await screen.findByLabelText("Describe it"), "something");
    await user.click(screen.getByRole("button", { name: "Fill in the form" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("took too long");
    expect(screen.getByLabelText("Describe it")).toHaveValue("something");
  });

  test("the header says the two system helpers share the model", async () => {
    mount({ "GET /architect": SEEDED_STATE });
    expect(await screen.findByTestId("architect-model")).toHaveTextContent(
      "The Architect uses two system helpers for the form; they share this model.",
    );
  });

  test("the header shows the model and the picker sets it through PUT /architect/model and refreshes", async () => {
    let model = "litellm:qwen3.8";
    const api = mount({
      "GET /architect": () => ({ body: { ...SEEDED_STATE, model } }),
      "PUT /architect/model": (body: unknown) => {
        model = (body as { model: string }).model;
        return { body: { model, agent_ids: ["a", "b", "c"] } };
      },
    });
    expect(await screen.findByTestId("architect-model")).toHaveTextContent("Model: litellm:qwen3.8");
    const picker = screen.getByLabelText("Change model");
    expect(optionsOf(picker).map((o) => (o as HTMLOptionElement).value)).toEqual(["litellm:qwen3.8", "litellm:other"]);
    chooseValue(picker, "litellm:other");
    await waitFor(() => expect(screen.getByTestId("architect-model")).toHaveTextContent("Model: litellm:other"));
    expect(api.calls.find((c) => c.key === "PUT /architect/model")?.body).toEqual({ model: "litellm:other" });
  });

  test("a refused model change is shown and the model stays", async () => {
    mount({
      "GET /architect": SEEDED_STATE,
      "PUT /architect/model": () => ({ status: 422, body: { detail: "The model 'litellm:other' is not available.", code: "model_not_allowed" } }),
    });
    await screen.findByTestId("architect-model");
    chooseValue(screen.getByLabelText("Change model"), "litellm:other");
    expect(await screen.findByRole("alert")).toHaveTextContent("not available");
    expect(screen.getByTestId("architect-model")).toHaveTextContent("Model: litellm:qwen3.8");
  });

  test("an invalid drafter answer is a readable alert: raw answer collapsed, the sentence, the picker inline", async () => {
    const api = mount({
      "GET /architect": SEEDED_STATE,
      "POST /architect/draft": () => ({
        status: 422,
        body: { detail: "The drafter's answer is not a valid agent definition.", code: "draft_invalid", raw: "Sure! Here is a poem.", problems: ["the answer is not JSON"] },
      }),
      "PUT /architect/model": { model: "litellm:other", agent_ids: [] },
    });
    await user.type(await screen.findByLabelText("Describe it"), "something");
    await user.click(screen.getByRole("button", { name: "Fill in the form" }));
    const alert = await screen.findByTestId("architect-drafter-failure");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("The drafter's model did not produce the form. Choose another model.");
    const details = alert.querySelector("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details).toHaveTextContent("Sure! Here is a poem.");
    expect(alert).not.toHaveTextContent(/retr(y|ies)/i);
    chooseValue(within(alert).getByLabelText("Drafter model"), "litellm:other");
    await waitFor(() => expect(api.calls.some((c) => c.key === "PUT /architect/model")).toBe(true));
    expect(screen.getByLabelText("Describe it")).toHaveValue("something");
  });

  test("a failed drafter run never shows the chat-style retry text outside the collapsed details", async () => {
    mount({
      "GET /architect": SEEDED_STATE,
      "POST /architect/draft": () => ({
        status: 502,
        body: { detail: "The drafter run did not finish.", code: "draft_failed", run_id: "r1", error: "Invalid structured output within the allowed retries" },
      }),
    });
    await user.type(await screen.findByLabelText("Describe it"), "something");
    await user.click(screen.getByRole("button", { name: "Fill in the form" }));
    const alert = await screen.findByTestId("architect-drafter-failure");
    expect(alert).toHaveTextContent("Choose another model.");
    const details = alert.querySelector("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    const visible = alert.cloneNode(true) as HTMLElement;
    visible.querySelector("details")?.remove();
    expect(visible).not.toHaveTextContent(/retr(y|ies)|structured output|did not finish/i);
  });

  test("the Chat and Workflow tabs are present", async () => {
    mount({ "GET /architect": SEEDED_STATE });
    await screen.findByRole("tab", { name: "Form" });
    await user.click(screen.getByRole("tab", { name: "Chat" }));
    expect(await screen.findByTestId("chat-tab")).toHaveAttribute("data-agent", "row-arch");
    await user.click(screen.getByRole("tab", { name: "Workflow" }));
    expect(await screen.findByLabelText("Describe the workflow")).toBeInTheDocument();
  });
});
