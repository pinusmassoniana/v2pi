import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetClock } from "../../api/clock";
import { AlertBanner } from "./AlertBanner";
import { CardHeader } from "./CardHeader";
import { Chip } from "./Chip";
import { KeyValueRows } from "./KeyValueRows";
import { Kpi } from "./Kpi";
import { Sparkline } from "./Sparkline";
import { StatusOrb } from "./StatusOrb";
import { Uptime } from "./Uptime";

const NOW = 1_700_000_100;

// Declared at module scope: counts how often the page around an Uptime renders.
function Page({ onRender, since }: { onRender: () => void; since: number }) {
  onRender();
  return <Uptime since={since} running />;
}

afterEach(() => {
  vi.useRealTimers();
  resetClock();
});

describe("Kpi", () => {
  it("is a region named by its label, with value, unit, sub-line and an aside", () => {
    render(<Kpi label="↓ Download" value="12.4" unit="Mbit/s" sub="last 5 min" aside={<Chip tone="ok">live</Chip>} />);
    const card = screen.getByRole("region", { name: "↓ Download" });
    expect(within(card).getByRole("heading", { level: 2, name: "↓ Download" })).toBeInTheDocument();
    expect(card).toHaveTextContent("12.4Mbit/s");
    expect(card).toHaveTextContent("last 5 min");
    expect(within(card).getByText("live")).toHaveAttribute("data-tone", "ok");
    expect(card).not.toHaveAttribute("data-dim");
  });

  it("draws a sparkline only from two points, and dims a stale value", () => {
    const { container, rerender } = render(<Kpi label="Latency" value="—" spark={[5]} dim />);
    expect(container.querySelector("svg")).toBeNull();
    expect(screen.getByRole("region", { name: "Latency" })).toHaveAttribute("data-dim", "true");
    rerender(<Kpi label="Latency" value="42" spark={[40, 42, 41]} />);
    expect(container.querySelector("svg path")).not.toBeNull();
  });

  it("a KPI inside a section with its own h2 titles itself one level down", () => {
    render(<Kpi label="Real" value="42" level={3} />);
    expect(screen.getByRole("heading", { level: 3, name: "Real" })).toBeInTheDocument();
  });

  it("colours the value by tone", () => {
    render(<Kpi label="Failovers · 24h" value="2" tone="warn" />);
    expect(screen.getByText("2")).toHaveClass("text-warn");
  });
});

describe("Chip", () => {
  it("carries its tone for the dot, and a plain chip is tinted instead", () => {
    render(<><Chip tone="bad">xray <b>STOPPED</b></Chip><Chip tone="bad" plain>stale config</Chip></>);
    expect(screen.getByText("STOPPED").parentElement).toHaveAttribute("data-tone", "bad");
    expect(screen.getByText("stale config")).toHaveClass("text-bad");
  });
});

describe("AlertBanner", () => {
  it("a bad banner is a group whose title alone is the alert; its action runs and shows a busy label", async () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <AlertBanner tone="bad" title="Config drift" text="· xray runs another config" action={{ label: "Reload config", busyLabel: "Reloading…", onClick }} />,
    );
    const banner = screen.getByRole("group", { name: "Config drift" });
    expect(banner).toHaveTextContent("Config drift · xray runs another config");
    expect(within(banner).getByRole("alert", { name: "Config drift" })).toHaveTextContent(/^Config drift$/);
    await userEvent.click(within(banner).getByRole("button", { name: "Reload config" }));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(within(banner).queryByRole("button", { name: /Dismiss/ })).toBeNull();

    rerender(<AlertBanner tone="bad" title="Config drift" action={{ label: "Reload config", busyLabel: "Reloading…", busy: true, onClick }} />);
    expect(screen.getByRole("button", { name: "Reloading…" })).toBeDisabled();
  });

  it("a warning's title is the status; the sentence that ticks is outside it and not live", async () => {
    const onDismiss = vi.fn();
    const { rerender } = render(<AlertBanner tone="warn" title="Auto-failover" text="to nl-ams-03 · 10m ago" onDismiss={onDismiss}><span>extra</span></AlertBanner>);
    const banner = screen.getByRole("group", { name: "Auto-failover" });
    expect(banner).toHaveTextContent("extra");
    const live = within(banner).getByRole("status", { name: "Auto-failover" });
    expect(live).toHaveTextContent(/^Auto-failover$/);
    expect(within(banner).queryByRole("alert")).toBeNull();
    const age = within(banner).getByText("to nl-ams-03 · 10m ago");
    expect(age).toHaveAttribute("aria-live", "off");
    expect(live).not.toContainElement(age);
    rerender(<AlertBanner tone="warn" title="Auto-failover" text="to nl-ams-03 · 11m ago" onDismiss={onDismiss}><span>extra</span></AlertBanner>);
    expect(live).toHaveTextContent(/^Auto-failover$/);
    await userEvent.click(within(banner).getByRole("button", { name: "Dismiss: Auto-failover" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});

describe("Sparkline", () => {
  it("is decorative, draws nothing for fewer than two points, and a gradient that survives a flat line", () => {
    const { container, rerender } = render(<Sparkline values={[3]} />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg.querySelector("path")).toBeNull();
    rerender(<Sparkline values={[7, 7, 7]} />);
    expect(svg.querySelector("path")!.getAttribute("d")).toBe("M0.0,36.0 L100.0,36.0 L200.0,36.0");
    expect(svg.querySelector("linearGradient")).toHaveAttribute("gradientUnits", "userSpaceOnUse");
    rerender(<Sparkline values={[1, 2]} series="up" />);
    expect(svg.querySelector("path")!.style.stroke).toBe("var(--series-up)");
  });

  it("two sparklines never share a gradient id", () => {
    const { container } = render(<><Sparkline values={[1, 2]} /><Sparkline values={[2, 1]} /></>);
    const ids = [...container.querySelectorAll("linearGradient")].map((g) => g.id);
    expect(new Set(ids).size).toBe(2);
    expect(ids.every((id) => /^spark-[a-zA-Z0-9_-]+$/.test(id))).toBe(true);
  });
});

describe("KeyValueRows", () => {
  it("renders each key with its value and optional sub-line", () => {
    render(<KeyValueRows rows={[{ key: "Segment", value: "10.0.2.1 · eth0.2" }, { key: "DHCP pool", value: "10.0.2.100–10.0.2.149", sub: "12 clients · pool 50" }]} />);
    const terms = screen.getAllByRole("term").map((t) => t.textContent);
    expect(terms).toEqual(["Segment", "DHCP pool"]);
    expect(screen.getAllByRole("definition").map((d) => d.textContent)).toEqual(["10.0.2.1 · eth0.2", "10.0.2.100–10.0.2.149", "12 clients · pool 50"]);
  });
});

describe("CardHeader", () => {
  it("titles the card with a level-2 heading and places the aside", () => {
    render(<CardHeader title="Routing" detail="· 4 of 6 rules" aside={<a href="#/tunnel/routing">Tunnel › Routing →</a>} />);
    expect(screen.getByRole("heading", { level: 2, name: "Routing" })).toBeInTheDocument();
    expect(screen.getByText("· 4 of 6 rules")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tunnel › Routing →" })).toBeInTheDocument();
  });

  it("a card inside a sheet or a page part titles itself with a level-3 heading", () => {
    render(<CardHeader title="Health" level={3} />);
    expect(screen.getByRole("heading", { level: 3, name: "Health" })).toBeInTheDocument();
  });
});

describe("Uptime", () => {
  it("ticks every second on the gateway clock without re-rendering the page around it", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW * 1000));
    resetClock();
    const onRender = vi.fn();
    render(<Page onRender={onRender} since={NOW - 3 * 86_400 - 65} />);
    expect(screen.getByText("3d 00:01:05")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(screen.getByText("3d 00:01:07")).toBeInTheDocument();
    expect(onRender).toHaveBeenCalledTimes(1);
  });

  it("shows a dash while not running or without a start time, and stops its timer", () => {
    vi.useFakeTimers();
    const { rerender } = render(<Uptime since={NOW} running={false} />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
    rerender(<Uptime since={null} running />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("coarse: '3d 4h', refreshed every 30 s", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW * 1000));
    resetClock();
    render(<Uptime since={NOW - (3 * 86_400 + 4 * 3_600 + 59 * 60 + 50)} running coarse />);
    expect(screen.getByText("3d 4h")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(29_000); });
    expect(screen.getByText("3d 4h")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(screen.getByText("3d 5h")).toBeInTheDocument();
  });
});

describe("StatusOrb", () => {
  it("names the state for assistive tech and carries its tone", () => {
    render(<><StatusOrb value="42" caption="ms · ONLINE" tone="ok" /><StatusOrb value="×" caption="OFFLINE" tone="bad" /></>);
    expect(screen.getByRole("img", { name: "42 ms · ONLINE" })).toHaveAttribute("data-tone", "ok");
    expect(screen.getByRole("img", { name: "× OFFLINE" })).toHaveAttribute("data-tone", "bad");
  });
});
