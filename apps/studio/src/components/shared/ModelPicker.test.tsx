import React, { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import type { ModelOption } from "@/components/agents/specSections";
import type { ProviderStatus } from "@/lib/providers";
import { ModelPicker } from "./ModelPicker";
import { getSelectOption, optionsOf } from "@/test/select";

afterEach(cleanup);

const pick = (select: HTMLElement, value: string) => fireEvent.change(select, { target: { value } });
const open = (el: HTMLElement) => fireEvent.click(el);


const model = (provider: string, name: string, extra: Partial<ModelOption> = {}): ModelOption => ({
  id: `${provider}:${name}`, provider, name, wildcard: false, source: "discovered", description: null, ...extra,
});
const MODELS: ModelOption[] = [
  model("litellm", "qwen3.8"),
  model("litellm", "llama-3"),
  model("openai", "gpt-4.1"),
  model("test", "echo", { source: "scripted" }),
  model("anthropic", "*", { wildcard: true, source: "allowlist", discovery_error: "could not connect to the provider" }),
  model("bedrock", "*", { wildcard: true, source: "allowlist" }),
];
const status = (id: string, state: ProviderStatus["status"], count = 0, error: string | null = null): ProviderStatus => ({
  id, label: id, configured: state !== "not_configured", env_var: "X_KEY", base_url_host: "h.test", status: state,
  model_count: count, checked_at: null, error,
});
const PROVIDERS = [status("litellm", "ok", 2), status("openai", "unreachable", 0, "the provider rejected the server's key"), status("anthropic", "not_configured")];

function Harness({ initial = { provider: "", name: "" }, models = MODELS, loadError = null as string | null, onChange = vi.fn() }) {
  const [value, setValue] = useState(initial);
  return (
    <ModelPicker
      providerId="p" modelId="m" provider={value.provider} name={value.name} models={models}
      loadError={loadError} providers={PROVIDERS}
      onChange={(next) => { onChange(next); setValue(next); }}
    />
  );
}

describe("ModelPicker", () => {
  it("lists the providers with their status and filters the models by the chosen provider", async () => {
        const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const provider = screen.getByLabelText("Model provider");
    expect(optionsOf(provider).map((o) => o.textContent)).toEqual([
      "Select a provider…",
      "litellm (Connected, 2 models)",
      "openai (Unreachable)",
      "test",
      "anthropic (Not configured)",
      "bedrock",
    ]);
    expect(screen.getByLabelText("Model name")).toBeDisabled();
    pick(provider, "litellm");
    expect(onChange).toHaveBeenLastCalledWith({ provider: "litellm", name: "" });
    expect(screen.getByTestId("model-picker-status")).toHaveTextContent("Connected");
    open(screen.getByLabelText("Model name"));
    await waitFor(() => expect(within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.textContent)).toEqual(["qwen3.8", "llama-3"])); // not openai's
  });

  it("searches the models and selects one", async () => {
        const onChange = vi.fn();
    render(<Harness initial={{ provider: "litellm", name: "" }} onChange={onChange} />);
    open(screen.getByLabelText("Model name"));
    fireEvent.change(await screen.findByPlaceholderText("Search litellm…"), { target: { value: "llama" } });
    await waitFor(() => expect(within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.textContent)).toEqual(["llama-3"]));
    fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: "llama-3" }));
    expect(onChange).toHaveBeenLastCalledWith({ provider: "litellm", name: "llama-3" });
    expect(screen.getByLabelText("Model name")).toHaveTextContent("llama-3");
  });

  it("works from the keyboard: arrows and Enter choose, Escape closes and returns focus", async () => {
    const onChange = vi.fn();
    render(<Harness initial={{ provider: "litellm", name: "" }} onChange={onChange} />);
    const trigger = screen.getByLabelText("Model name");
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(trigger); // a button's Enter is a click in a browser
    const search = await screen.findByPlaceholderText("Search litellm…");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(search, { key: "ArrowDown" });
    fireEvent.keyDown(search, { key: "Enter" });
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith({ provider: "litellm", name: "llama-3" }));
    open(screen.getByLabelText("Model name"));
    fireEvent.keyDown(await screen.findByPlaceholderText("Search litellm…"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
    expect(screen.getByLabelText("Model name")).toHaveFocus();
  });

  it("clears the model when the provider changes, and keeps a stored model the list lacks, marked", async () => {
        const onChange = vi.fn();
    render(<Harness initial={{ provider: "litellm", name: "old-model" }} onChange={onChange} />);
    open(screen.getByLabelText("Model name"));
    expect(await within(await screen.findByRole("listbox")).findByRole("option", { name: "old-model (not listed)" })).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    pick(screen.getByLabelText("Model provider"), "openai");
    expect(onChange).toHaveBeenLastCalledWith({ provider: "openai", name: "" });
  });

  it("marks a stored provider the list does not offer", () => {
    render(<Harness initial={{ provider: "mistral", name: "x" }} />);
    expect(getSelectOption("mistral (not in the allowlist)")).toBeTruthy();
  });

  it("falls back to free text, with the reason, for a provider whose list could not be discovered", async () => {
        const onChange = vi.fn();
    render(<Harness initial={{ provider: "anthropic", name: "" }} onChange={onChange} />);
    const input = screen.getByLabelText("Model name");
    expect(input.tagName).toBe("INPUT");
    expect(screen.getByTestId("model-picker-hint")).toHaveTextContent("The model list for anthropic is unavailable (could not connect to the provider). Type a model name.");
    fireEvent.change(input, { target: { value: "c" } });
    expect(onChange).toHaveBeenLastCalledWith({ provider: "anthropic", name: "c" });
    expect(screen.getByTestId("model-picker-status")).toHaveTextContent("Not configured");
  });

  it("falls back to free text for a plain wildcard provider", () => {
    render(<Harness initial={{ provider: "bedrock", name: "" }} />);
    expect(screen.getByLabelText("Model name").tagName).toBe("INPUT");
    expect(screen.getByTestId("model-picker-hint")).toHaveTextContent("Any model name on bedrock is allowed");
  });

  it("is free text for both fields when the model list could not be loaded", async () => {
    const onChange = vi.fn();
    render(<Harness models={null as unknown as ModelOption[]} loadError="boom" onChange={onChange} />);
    expect(screen.getByLabelText("Model provider").tagName).toBe("INPUT");
    expect(screen.getByRole("alert")).toHaveTextContent("The model list could not be loaded (boom)");
    fireEvent.change(screen.getByLabelText("Model name"), { target: { value: "x" } });
    expect(onChange).toHaveBeenLastCalledWith({ provider: "", name: "x" });
  });

  it("keeps focus and the typed text when the list arrives mid-typing, and switches once focus leaves", () => {
    const onChange = vi.fn();
    const { rerender } = render(<Harness models={null as unknown as ModelOption[]} onChange={onChange} />);
    expect(screen.getByTestId("model-picker-hint")).toHaveTextContent("Loading the model list…");
    const providerInput = screen.getByLabelText("Model provider");
    providerInput.focus();
    fireEvent.change(providerInput, { target: { value: "litellm" } });
    const modelInput = screen.getByLabelText("Model name");
    modelInput.focus(); // moving between the two fields keeps the hold
    fireEvent.change(modelInput, { target: { value: "qwe" } });

    rerender(<Harness models={MODELS} onChange={onChange} />);
    expect(screen.getByLabelText("Model name")).toBe(modelInput); // the same element, not replaced
    expect(modelInput).toHaveFocus();
    expect(modelInput).toHaveValue("qwe");
    expect(screen.getByLabelText("Model provider")).toHaveValue("litellm");
    expect(screen.getByTestId("model-picker-hint")).toHaveTextContent("The model list has loaded. It replaces these fields when you leave them.");
    fireEvent.change(modelInput, { target: { value: "qwen3.8" } });
    expect(onChange).toHaveBeenLastCalledWith({ provider: "litellm", name: "qwen3.8" });

    act(() => modelInput.blur());
    expect(screen.getByTestId("model-picker")).toHaveAttribute("data-mode", "list");
    expect(screen.getByLabelText("Model provider")).toHaveAttribute("data-select-trigger");
    expect(screen.getByLabelText("Model provider")).toHaveValue("litellm");
    expect(screen.getByLabelText("Model name")).toHaveTextContent("qwen3.8");
  });

  it("shows the list at once when it arrives while the free-text fields are not focused", () => {
    const { rerender } = render(<Harness models={null as unknown as ModelOption[]} />);
    rerender(<Harness models={MODELS} />);
    expect(screen.getByLabelText("Model provider")).toHaveAttribute("data-select-trigger");
  });

  it("shows the problem under the model and links it to the field", () => {
    render(
      <ModelPicker providerId="p" modelId="m" provider="litellm" name="" models={MODELS} providers={PROVIDERS} problem="Choose a model" onChange={() => undefined} />,
    );
    expect(screen.getByLabelText("Model name")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Choose a model")).toBeInTheDocument();
  });

  it("fits 375 px: one column, nothing sized in pixels", () => {
    const restore = setWindowWidth(375);
    try {
      const { container } = render(<Harness initial={{ provider: "litellm", name: "qwen3.8" }} />);
      const grid = container.querySelector(".grid")!;
      expect(grid.className).toContain("grid-cols-1");
      expect(grid.className).toContain("sm:grid-cols-2");
      for (const el of container.querySelectorAll("select, input, button")) expect(el.className).toContain("min-w-0");
    } finally {
      restore();
    }
  });

  it("has no axe violations (list, free text and open list)", async () => {
        const { container, unmount } = render(<Harness initial={{ provider: "litellm", name: "qwen3.8" }} />);
    await expectNoAxeViolations(container);
    open(screen.getByLabelText("Model name"));
    await screen.findByPlaceholderText("Search litellm…");
    await expectNoAxeViolations(document.body);
    unmount();
    const free = render(<Harness initial={{ provider: "anthropic", name: "" }} />);
    await expectNoAxeViolations(free.container);
  }, AXE_TIMEOUT_MS);

  it("loads the provider statuses itself when none are passed", async () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: PROVIDERS }), { status: 200, headers: { "content-type": "application/json" } })));
    try {
      render(<ModelPicker providerId="p" modelId="m" provider="litellm" name="qwen3.8" models={MODELS} onChange={() => undefined} />);
      await waitFor(() => expect(screen.getByTestId("model-picker-status")).toHaveTextContent("Connected"));
    } finally {
      vi.unstubAllGlobals();
      vi.unstubAllEnvs();
    }
  });
});
