import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Elapsed, elapsedLabel } from "./Elapsed";

afterEach(() => vi.useRealTimers());

describe("Elapsed", () => {
  it("counts whole seconds on its own clock", () => {
    vi.useFakeTimers();
    render(<Elapsed since={Date.now()} />);
    expect(screen.getByText("0 s")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(42_000); });
    expect(screen.getByText("42 s")).toBeInTheDocument();
  });

  it("as a clock, minutes and two-digit seconds, and it is never a live region", () => {
    vi.useFakeTimers();
    render(<Elapsed since={Date.now()} format="clock" />);
    expect(screen.getByText("0:00")).toHaveAttribute("aria-live", "off");
    act(() => { vi.advanceTimersByTime(34_000); });
    expect(screen.getByText("0:34")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(92_000); });
    expect(screen.getByText("2:06")).toBeInTheDocument();
  });

  it("labels", () => {
    expect(elapsedLabel(999)).toBe("0 s");
    expect(elapsedLabel(61_500, "clock")).toBe("1:01");
    expect(elapsedLabel(600_000, "clock")).toBe("10:00");
    expect(elapsedLabel(-5, "clock")).toBe("0:00");
  });
});
