import React, { useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Select } from "./Select";

function Harness({ onChange }: { onChange?: (value: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <Select
      aria-label="Kind"
      value={value}
      onValueChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
    >
      <option value="">All kinds</option>
      <option value="tool">Tool approval</option>
      <option value="plan">Plan review</option>
    </Select>
  );
}

describe("Select selection, real-browser event timing", () => {
  // A browser drains the microtask queue after every listener returns; jsdom's synchronous dispatch
  // only does so once the whole dispatch is over, which is why a flag cleared by a microtask passed
  // here while real Chrome ignored mouse selection. Emulate the browser by draining after each listener.
  const realAdd = EventTarget.prototype.addEventListener;
  const realRemove = EventTarget.prototype.removeEventListener;
  const wrappers = new WeakMap<object, EventListener>();
  const realQueue = globalThis.queueMicrotask;
  let pending: Array<() => void> = [];
  beforeEach(() => {
    pending = [];
    globalThis.queueMicrotask = (fn) => void pending.push(fn);
    EventTarget.prototype.addEventListener = function (this: EventTarget, type: string, listener: unknown, options?: unknown) {
      if (typeof listener !== "function") return realAdd.call(this, type, listener as EventListener, options as boolean);
      let wrapped = wrappers.get(listener);
      if (!wrapped) {
        wrapped = function (this: unknown, event: Event) {
          try {
            return (listener as (e: Event) => unknown).call(this, event);
          } finally {
            const run = pending;
            pending = [];
            run.forEach((fn) => fn());
          }
        } as EventListener;
        wrappers.set(listener, wrapped);
      }
      return realAdd.call(this, type, wrapped, options as boolean);
    } as typeof EventTarget.prototype.addEventListener;
    EventTarget.prototype.removeEventListener = function (this: EventTarget, type: string, listener: unknown, options?: unknown) {
      const wrapped = typeof listener === "function" ? wrappers.get(listener) : undefined;
      return realRemove.call(this, type, (wrapped ?? listener) as EventListener, options as boolean);
    } as typeof EventTarget.prototype.removeEventListener;
  });
  afterEach(() => {
    EventTarget.prototype.addEventListener = realAdd;
    EventTarget.prototype.removeEventListener = realRemove;
    globalThis.queueMicrotask = realQueue;
  });

  it("mouse: pointerdown on the trigger, pointerup on an item", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const user = userEvent.setup();
    const trigger = screen.getByRole("combobox", { name: "Kind" });
    await user.pointer({ keys: "[MouseLeft>]", target: trigger });
    const option = await screen.findByRole("option", { name: "Plan review" });
    // A real pointer travels to the item first (Radix reads the pointer type off that move) and, being
    // more than 10px from where it pressed, is not mistaken for the click that opened the list.
    const coords = { pageX: 120, pageY: 120, clientX: 120, clientY: 120 };
    await user.pointer({ target: option, coords });
    await user.pointer({ keys: "[/MouseLeft]", target: option, coords });
    expect(onChange).toHaveBeenCalledWith("plan");
    await waitFor(() => expect(trigger).toHaveTextContent("Plan review"));
  });

  it("mouse: click opens, click on an item picks", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const user = userEvent.setup();
    const trigger = screen.getByRole("combobox", { name: "Kind" });
    await user.click(trigger);
    await user.click(await screen.findByRole("option", { name: "Tool approval" }));
    expect(onChange).toHaveBeenCalledWith("tool");
    await waitFor(() => expect(trigger).toHaveTextContent("Tool approval"));
  });

  it("touch: tap on an item picks", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const user = userEvent.setup();
    const trigger = screen.getByRole("combobox", { name: "Kind" });
    await user.pointer({ keys: "[TouchA]", target: trigger });
    const option = await screen.findByRole("option", { name: "Plan review" });
    await user.pointer({ keys: "[TouchA]", target: option });
    expect(onChange).toHaveBeenCalledWith("plan");
  });

  it("keyboard: Enter on a focused item picks", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const user = userEvent.setup();
    const trigger = screen.getByRole("combobox", { name: "Kind" });
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    const option = await screen.findByRole("option", { name: "Plan review" });
    act(() => option.focus());
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith("plan");
  });
});
