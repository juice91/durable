import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { JsonSchemaForm, numberBounds } from "./JsonSchemaForm";

describe("numberBounds (browser check: number fields reported a max below their min)", () => {
  it("passes a schema's consistent bounds, and drops an inverted pair", () => {
    expect(numberBounds({ type: "integer" })).toEqual({});
    expect(numberBounds({ type: "integer", minimum: 1, maximum: 5 })).toEqual({ min: 1, max: 5 });
    expect(numberBounds({ type: "number", minimum: 0 })).toEqual({ min: 0 });
    expect(numberBounds({ type: "number", maximum: 9 })).toEqual({ max: 9 });
    expect(numberBounds({ type: "integer", minimum: 5, maximum: 1 })).toEqual({});
  });

  it("a schema form number input carries them", () => {
    render(
      <JsonSchemaForm
        schema={{ type: "object", properties: { seats: { type: "integer", minimum: 1, maximum: 50 }, ratio: { type: "number" } } }}
        value={{}}
        onChange={() => undefined}
      />,
    );
    const seats = screen.getByLabelText(/Seats/) as HTMLInputElement;
    expect([seats.min, seats.max]).toEqual(["1", "50"]);
    const ratio = screen.getByLabelText(/Ratio/) as HTMLInputElement;
    expect([ratio.min, ratio.max]).toEqual(["", ""]);
  });
});

describe("unbounded numbers are text fields (browser re-check: Chrome reported min = max = 0)", () => {
  function Harness({ initial = {} as Record<string, unknown>, onValue }: { initial?: Record<string, unknown>; onValue: (v: Record<string, unknown>) => void }) {
    const [value, setValue] = React.useState<Record<string, unknown>>(initial);
    return (
      <>
        <JsonSchemaForm
          schema={{ type: "object", properties: { tier: { type: "integer" }, ratio: { type: "number" } } }}
          value={value as never}
          onChange={(next) => {
            setValue(next as Record<string, unknown>);
            onValue(next as Record<string, unknown>);
          }}
        />
        <button type="button" onClick={() => setValue({ tier: 7, ratio: 0.25 })}>
          Set from JSON
        </button>
      </>
    );
  }

  it("exposes no spinbutton, keeps the typed text (a decimal point survives) and emits numbers", () => {
    const seen: Record<string, unknown>[] = [];
    render(<Harness onValue={(v) => seen.push(v)} />);
    expect(screen.queryByRole("spinbutton")).toBeNull();
    const tier = screen.getByLabelText("Tier") as HTMLInputElement;
    expect(tier).toHaveAttribute("inputmode", "numeric");
    expect(tier).not.toHaveAttribute("aria-valuemin");
    const ratio = screen.getByLabelText("Ratio") as HTMLInputElement;
    fireEvent.change(ratio, { target: { value: "1." } });
    expect(ratio.value).toBe("1.");
    expect(seen.at(-1)).toMatchObject({ ratio: 1 });
    fireEvent.change(ratio, { target: { value: "1.5" } });
    expect(ratio.value).toBe("1.5");
    expect(seen.at(-1)).toMatchObject({ ratio: 1.5 });
    fireEvent.change(tier, { target: { value: "3" } });
    expect(seen.at(-1)).toMatchObject({ tier: 3 });
    expect(typeof seen.at(-1)?.tier).toBe("number");
  });

  it("still checks the number: text that is not one is kept and flagged", () => {
    render(<Harness onValue={() => undefined} />);
    const tier = screen.getByLabelText("Tier");
    fireEvent.change(tier, { target: { value: "abc" } });
    fireEvent.blur(tier);
    expect(tier).toHaveValue("abc");
    expect(screen.getByText("Tier must be an integer")).toBeInTheDocument();
    const ratio = screen.getByLabelText("Ratio");
    fireEvent.change(ratio, { target: { value: "x" } });
    fireEvent.blur(ratio);
    expect(screen.getByText("Ratio must be a number")).toBeInTheDocument();
  });

  it("a value set from outside (JSON mode) replaces what was typed", () => {
    render(<Harness onValue={() => undefined} />);
    const ratio = screen.getByLabelText("Ratio") as HTMLInputElement;
    fireEvent.change(ratio, { target: { value: "2." } });
    fireEvent.click(screen.getByRole("button", { name: "Set from JSON" }));
    expect(ratio.value).toBe("0.25");
    expect((screen.getByLabelText("Tier") as HTMLInputElement).value).toBe("7");
  });
});
