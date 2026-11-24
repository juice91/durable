import { describe, expect, test } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Avatar } from "./Avatar";

describe("Avatar", () => {
  test("shows the initial without a picture", () => {
    render(<Avatar name="joyce@example.com" />);
    expect(screen.getByTestId("avatar-initial")).toHaveTextContent("J");
  });

  test("shows the picture with alt text and no referrer, and falls back to the initial when it fails to load", () => {
    render(<Avatar name="Joyce" src="https://cdn.test/me.png" />);
    const img = screen.getByAltText("Joyce's picture");
    expect(img).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(img).toHaveAttribute("width", "24");
    fireEvent.error(img);
    expect(screen.getByTestId("avatar-initial")).toHaveTextContent("J");
    expect(screen.queryByRole("img")).toBeNull();
  });
});
