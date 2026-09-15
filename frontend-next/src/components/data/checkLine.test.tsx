import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { runKey } from "../../lib/staleResult";
import { CheckLine } from "./CheckLine";

const key = runKey({ rules: [] });

describe("CheckLine", () => {
  it("keeps an empty status mounted until something ran, so the answer arrives in a live region that already exists", () => {
    const { rerender } = render(<CheckLine result={null} liveKey={key} />);
    const line = screen.getByRole("status");
    expect(line).toBeEmptyDOMElement();
    rerender(<CheckLine result={{ ok: true, text: "✓ ruleset valid", key }} liveKey={key} />);
    expect(screen.getByRole("status")).toBe(line);
    expect(line).toHaveTextContent("✓ ruleset valid");
  });

  it("a pass reads in the ok colour, a failure in the bad one, both as a status", () => {
    const { rerender } = render(<CheckLine result={{ ok: true, text: "✓ ruleset valid", key }} liveKey={key} />);
    expect(screen.getByRole("status")).toHaveTextContent("✓ ruleset valid");
    expect(screen.getByRole("status")).toHaveClass("text-ok");
    rerender(<CheckLine result={{ ok: false, text: "✗ rule 1: empty value", key }} liveKey={key} />);
    expect(screen.getByRole("status")).toHaveClass("text-bad");
    expect(screen.getByRole("status")).toHaveAttribute("title", "✗ rule 1: empty value");
  });

  it("once the form moved past what it checked, it says so instead of the result", () => {
    render(<CheckLine result={{ ok: true, text: "✓ ruleset valid", key }} liveKey={runKey({ rules: [{ value: "ru" }] })} />);
    const line = screen.getByRole("status");
    expect(line).toHaveTextContent("Form changed since this run — run it again.");
    expect(line).toHaveAttribute("data-stale", "true");
    expect(line).toHaveClass("text-t3");
  });
});
