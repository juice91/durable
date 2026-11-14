import React from "react";
import { render, screen } from "@testing-library/react";
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
