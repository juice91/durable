import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { describe, expect, it } from "vitest";
import { JsonSchemaForm } from "./JsonSchemaForm";

const schema = {
  type: "object" as const,
  required: ["name", "count"],
  properties: {
    name: { type: "string" as const, title: "Name" },
    count: { type: "integer" as const, title: "Count" },
    ratio: { type: "number" as const, title: "Ratio" },
    active: { type: "boolean" as const, title: "Active" },
    mode: { type: "string" as const, title: "Mode", enum: ["fast", "safe"] },
    tags: { type: "array" as const, title: "Tags", items: { type: "string" as const } },
    options: {
      type: "object" as const,
      title: "Options",
      required: ["retries"],
      properties: { retries: { type: "integer" as const, title: "Retries" } },
    },
  },
};

describe("JsonSchemaForm", () => {
  it("generates controls for supported schema types and nested objects", () => {
    render(<JsonSchemaForm schema={schema} value={{}} onChange={() => undefined} />);

    expect(screen.getByLabelText(/Name \*/)).toHaveAttribute("type", "text");
    // Unbounded numbers are text fields with a numeric keyboard (Chrome reports min = max = 0 for a
    // type=number without bounds; browser re-check). Bounded ones stay spinbuttons (numberBounds test).
    expect(screen.getByLabelText(/Count \*/)).toHaveAttribute("type", "text");
    expect(screen.getByLabelText(/Count \*/)).toHaveAttribute("inputmode", "numeric");
    expect(screen.getByLabelText(/Count \*/)).not.toHaveRole("spinbutton");
    expect(screen.getByLabelText("Ratio")).toHaveAttribute("type", "text");
    expect(screen.getByLabelText("Ratio")).toHaveAttribute("inputmode", "decimal");
    expect(screen.getByLabelText("Active")).toHaveAttribute("type", "checkbox");
    expect(screen.getByLabelText("Mode")).toHaveRole("combobox");
    expect(screen.getByLabelText("Tags")).toHaveAttribute("placeholder", "One item per line");
    expect(screen.getByLabelText(/Retries \*/)).toBeInTheDocument();
  });

  it("shows inline required and numeric validation messages", () => {
    render(<JsonSchemaForm schema={schema} value={{ name: "", count: "nope" }} onChange={() => undefined} />);

    fireEvent.blur(screen.getByLabelText(/Name \*/));
    fireEvent.blur(screen.getByLabelText(/Count \*/));

    expect(screen.getByText("Name is required")).toBeInTheDocument();
    expect(screen.getByText("Count must be an integer")).toBeInTheDocument();
  });

  it("emits typed nested values when fields change", () => {
    let value: Record<string, unknown> = {};
    const { rerender } = render(
      <JsonSchemaForm schema={schema} value={value} onChange={(next) => { value = next; }} />,
    );

    fireEvent.change(screen.getByLabelText(/Count \*/), { target: { value: "4" } });
    rerender(<JsonSchemaForm schema={schema} value={value} onChange={(next) => { value = next; }} />);
    fireEvent.change(screen.getByLabelText("Tags"), { target: { value: "one\ntwo" } });
    rerender(<JsonSchemaForm schema={schema} value={value} onChange={(next) => { value = next; }} />);
    fireEvent.change(screen.getByLabelText(/Retries \*/), { target: { value: "2" } });

    expect(value).toEqual({ count: 4, tags: ["one", "two"], options: { retries: 2 } });
  });
});
