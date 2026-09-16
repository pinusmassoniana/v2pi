import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { setViewportWidth } from "../test/viewport";
import { DESKTOP_QUERY, useMediaQuery } from "./media";

function Width() {
  return <span>{useMediaQuery(DESKTOP_QUERY) ? "desktop" : "phone"}</span>;
}

describe("useMediaQuery", () => {
  it("answers for the current viewport and follows it across the 768 px breakpoint", () => {
    setViewportWidth(390);
    render(<Width />);
    expect(screen.getByText("phone")).toBeInTheDocument();
    act(() => setViewportWidth(768));
    expect(screen.getByText("desktop")).toBeInTheDocument();
    act(() => setViewportWidth(767));
    expect(screen.getByText("phone")).toBeInTheDocument();
  });

  it("reads false where the browser has no matchMedia", () => {
    const original = window.matchMedia;
    // @ts-expect-error — a browser without the API
    delete window.matchMedia;
    try {
      render(<Width />);
      expect(screen.getByText("phone")).toBeInTheDocument();
    } finally {
      window.matchMedia = original;
    }
  });
});
