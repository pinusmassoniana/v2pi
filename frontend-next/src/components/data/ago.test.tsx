import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { recordServerNow, resetClock } from "../../api/clock";
import { AGO_TICK_MS, Ago, useNow } from "./Ago";

const NOW_SEC = 1_700_000_100;

afterEach(() => {
  vi.useRealTimers();
  resetClock();
});

// Declared at module scope: one reader of the shared clock.
function Clock({ label }: { label: string }) {
  const now = useNow(AGO_TICK_MS);
  return <p>{label} {Math.floor(now / 1000)}</p>;
}

describe("useNow", () => {
  it("runs one interval for every reader, started by the first and stopped with the last", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW_SEC * 1000));
    const start = vi.spyOn(globalThis, "setInterval");
    const stop = vi.spyOn(globalThis, "clearInterval");
    const first = render(<Clock label="a" />);
    const second = render(<Clock label="b" />);
    const third = render(<Clock label="c" />);
    expect(start).toHaveBeenCalledTimes(1);
    expect(start).toHaveBeenCalledWith(expect.any(Function), AGO_TICK_MS);

    act(() => { vi.advanceTimersByTime(AGO_TICK_MS); });
    for (const label of ["a", "b", "c"]) expect(screen.getByText(`${label} ${NOW_SEC + 15}`)).toBeInTheDocument();

    first.unmount();
    second.unmount();
    expect(stop).not.toHaveBeenCalled();
    third.unmount();
    expect(stop).toHaveBeenCalledTimes(1);

    render(<Clock label="d" />);   // a new first reader starts it again
    expect(start).toHaveBeenCalledTimes(2);
  });
});

describe("Ago", () => {
  it("reads the gateway clock, moves on with the ticker, and says the fallback without a time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW_SEC * 1000));
    recordServerNow(NOW_SEC - 3_600);   // the gateway is an hour behind this browser
    render(
      <>
        <p data-testid="probed"><Ago at={new Date((NOW_SEC - 3_605) * 1000).toISOString()} /></p>
        <p data-testid="never"><Ago at={null} fallback="not probed" /></p>
        <p data-testid="unreadable"><Ago at="not a date" /></p>
      </>,
    );
    expect(screen.getByTestId("probed")).toHaveTextContent("5 s ago");
    expect(screen.getByTestId("never")).toHaveTextContent("not probed");
    expect(screen.getByTestId("unreadable")).toHaveTextContent("—");
    act(() => { vi.advanceTimersByTime(2 * 60_000); });
    expect(screen.getByTestId("probed")).toHaveTextContent("2 min ago");
  });
});
