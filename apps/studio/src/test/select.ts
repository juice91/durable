import { fireEvent, screen, waitFor, within } from "@testing-library/react";

/**
 * Picks an option in the themed `Select`: opens the trigger with the keyboard, clicks the option by
 * its visible name (string = exact text, RegExp = pattern) and waits for the list to close. One line
 * replaces `fireEvent.change(select, { target: { value } })`.
 */
export async function chooseOption(trigger: HTMLElement, name: string | RegExp): Promise<void> {
  fireEvent.keyDown(trigger, { key: "Enter" });
  // Radix mounts the list synchronously on the key press; `findBy`/`waitFor` are avoided on purpose:
  // with a Dialog open behind the list, jsdom's polling overflows the stack.
  const listbox = screen.getByRole("listbox");
  fireEvent.click(within(listbox).getByRole("option", { name }));
  if (screen.queryByRole("listbox")) throw new Error(`Select list still open after choosing "${String(name)}"`);
}

/** The value a themed `Select` currently holds (what `select.value` was on a native select). */
export function selectValue(trigger: HTMLElement): string {
  return trigger.getAttribute("data-value") ?? "";
}

/**
 * Picks the option whose value is `value` (what `fireEvent.change(select, { target: { value } })`
 * did on a native select). Use it when a test knows the value, `chooseOption` when it knows the text.
 */
export function chooseValue(trigger: HTMLElement, value: string): void {
  fireEvent.keyDown(trigger, { key: "Enter" });
  const listbox = screen.getByRole("listbox");
  const option = Array.from(listbox.querySelectorAll<HTMLElement>('[role="option"]')).find((el) => el.getAttribute("data-value") === value);
  if (!option) throw new Error(`No option with value "${value}" in the Select list`);
  fireEvent.click(option);
}

/**
 * Called from `setup.ts`: `fireEvent.change(trigger, { target: { value } })` on a themed Select picks
 * that option, so suites written for the native select keep their one-line interaction.
 */
export function installSelectChange(): void {
  const original = fireEvent.change;
  fireEvent.change = ((element: Element | Window | Document | Node, init?: { target?: { value?: unknown } }) => {
    if (element instanceof HTMLElement && element.hasAttribute("data-select-trigger") && init?.target && "value" in init.target) {
      chooseValue(element, String(init.target.value));
      return true;
    }
    return original(element, init as never);
  }) as typeof fireEvent.change;
}

export interface SelectOptionInfo {
  value: string;
  /** Visible text, as `option.textContent` was. */
  textContent: string;
  disabled: boolean;
  getAttribute(name: string): string | null;
}

/** The options of a themed Select, read from its closed trigger (what `select.options` was). */
export function optionsOf(trigger: HTMLElement): SelectOptionInfo[] {
  const raw = trigger.getAttribute("data-options");
  if (raw === null) throw new Error("Not a themed Select trigger (no data-options)");
  return (JSON.parse(raw) as Array<{ value: string; label: string; disabled: boolean }>).map((o) => ({
    value: o.value,
    textContent: o.label,
    disabled: o.disabled,
    getAttribute: (name: string) => (name === "value" ? o.value : null),
  }));
}

/**
 * Waits until an option called `name` exists: in the closed list of any themed Select or, for an
 * open list, as a real `option` role. Replaces `await screen.findByRole("option", { name })` used as
 * a "the select has loaded" signal.
 */
export async function findSelectOption(name: string | RegExp): Promise<void> {
  const matches = (label: string) => (typeof name === "string" ? label === name : name.test(label));
  await waitFor(() => {
    const closed = Array.from(document.querySelectorAll("[data-options]")).some((el) =>
      optionsOf(el as HTMLElement).some((o) => matches(o.textContent)),
    );
    if (!closed && screen.queryAllByRole("option", { name }).length === 0) throw new Error(`No option named ${String(name)}`);
  });
}

/**
 * The option called `name` in a closed themed Select (or in every Select under `scope`), or
 * `undefined`. Stands in for `getByRole("option", { name })` on a native select.
 */
export function getSelectOption(name: string | RegExp, scope: ParentNode = document): SelectOptionInfo | undefined {
  const matches = (label: string) => (typeof name === "string" ? label === name : name.test(label));
  const triggers = Array.from(scope.querySelectorAll<HTMLElement>("[data-options]"));
  if (scope instanceof HTMLElement && scope.hasAttribute("data-options")) triggers.push(scope);
  for (const trigger of triggers) {
    const found = optionsOf(trigger).find((o) => matches(o.textContent));
    if (found) return found;
  }
  return undefined;
}
