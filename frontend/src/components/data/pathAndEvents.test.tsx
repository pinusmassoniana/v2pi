import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetClock } from "../../api/clock";
import { ConnectedFor } from "./ConnectedFor";
import { ConnectionPath, type ConnectionPathProps } from "./ConnectionPath";
import { EventFeed } from "./EventFeed";
import type { NodeSlot } from "./types";

const NOW_SEC = 1_700_000_100;

const OK: NodeSlot = { state: "ok", leg: "ok", text: "42 ms", note: null, tone: "ok", ms: 42 };
const SLOW: NodeSlot = { state: "slow", leg: "ok", text: "831 ms", note: "slow", tone: "warn", ms: 831 };
const BAD: NodeSlot = { state: "bad", leg: "bad", text: "check failed", note: null, tone: "bad", ms: null };
const STALE: NodeSlot = { state: "stale", leg: "off", text: "health stale", note: null, tone: "neutral", ms: null };

const PATH: ConnectionPathProps = {
  label: "Connection path: devices, gateway, nl-ams-03, internet. Tunnel OK, 42 ms, down 12.4 Mbit/s, up 1.8 Mbit/s. Direct by routing rules: idle. Kill-switch ARMED.",
  clients: 12, poolSize: 50, gatewayIp: "10.0.2.1", gatewayIface: "eth0.2",
  nodeName: "nl-ams-03", nodeFlag: "🇳🇱", node: OK, connectedSince: null,
  egressIp: "185.107.56.21", egressIp6: "2a0b:4d07::21", uplink: true, uplink6: true, ipv6Enabled: true,
  rates: { proxy: { down: 12_400_000, up: 1_800_000 }, direct: { down: 0, up: 0 } },
  tunnelLines: ["↓ 12.4 Mbit/s", "↑ 1.8 Mbit/s"], directLines: ["idle"],
  killSwitch: { label: "ARMED", tone: "ok" },
};
const DIRECT_FLOWING: Partial<ConnectionPathProps> = {
  rates: { proxy: { down: 3_600_000, up: 400_000 }, direct: { down: 4_500_000, up: 300_000 } },
  tunnelLines: ["↓ 3.6 Mbit/s", "↑ 400 kbit/s"], directLines: ["↓ 4.5 Mbit/s", "↑ 300 kbit/s"],
};

const drawing = (container: HTMLElement, layout: "wide" | "narrow") => container.querySelector<SVGSVGElement>(`svg[data-layout="${layout}"]`)!;
const drawings = (container: HTMLElement) => [drawing(container, "wide"), drawing(container, "narrow")];
const columns = (container: HTMLElement) => [...container.querySelector("[data-stats]")!.children] as HTMLElement[];

afterEach(() => {
  vi.useRealTimers();
  resetClock();
});

describe("ConnectionPath", () => {
  it("one named image holds both drawings, each hidden from assistive tech, switched by the card's own width", () => {
    const { container } = render(<ConnectionPath {...PATH} />);
    const image = screen.getByRole("img");
    expect(image).toHaveAccessibleName(PATH.label);
    expect(image).not.toHaveAttribute("aria-live");
    expect(container.firstElementChild).toHaveClass("@container");
    const [wide, narrow] = drawings(container);
    expect(image).toContainElement(wide!);
    expect(image).toContainElement(narrow!);
    expect(wide).toHaveAttribute("aria-hidden", "true");
    expect(narrow).toHaveAttribute("aria-hidden", "true");
    expect(wide).toHaveAttribute("viewBox", "0 0 568 98");
    expect(narrow).toHaveAttribute("viewBox", "0 0 334 122");
    expect(wide).toHaveClass("block", "@max-md:hidden");
    expect(narrow).toHaveClass("hidden", "@max-md:block");
  });

  it("each drawing paints only with its own gradient, in user space, and nothing uses a filter", () => {
    const { container } = render(<ConnectionPath {...PATH} />);
    expect(container.querySelector("filter")).toBeNull();
    expect(container.querySelectorAll("[filter]")).toHaveLength(0);
    const gradients = [...container.querySelectorAll("linearGradient")];
    expect(gradients).toHaveLength(2);
    for (const gradient of gradients) expect(gradient).toHaveAttribute("gradientUnits", "userSpaceOnUse");
    for (const svg of drawings(container)) {
      const own = new Set([...svg.querySelectorAll("linearGradient")].map((g) => g.id));
      const used = [...svg.querySelectorAll("[style]")].flatMap((el) => /url\("?#([^")]+)"?\)/.exec(el.getAttribute("style")!)?.[1] ?? []);
      expect(used.length).toBeGreaterThan(0);
      for (const id of used) expect(own.has(id!)).toBe(true);
    }
  });

  it("details each point in the column under it; the flag is only in the node marker", () => {
    const { container } = render(<ConnectionPath {...PATH} />);
    expect(columns(container).map((c) => c.textContent)).toEqual([
      "Devices12clientspool 50",
      "Gateway10.0.2.1eth0.2kill-switch ARMED",
      "nl-ams-0342 msconnected —egress185.107.56.212a0b:4d07::21",
      "Internetuplink v4 ✓up v6 ✓up",
    ]);
    expect(within(columns(container)[2]!).getByText("nl-ams-03")).toHaveAttribute("title", "nl-ams-03");
    expect(within(columns(container)[1]!).getByText("ARMED")).toHaveClass("text-ok");
    for (const svg of drawings(container)) {
      expect(svg.querySelector("[data-node]")!.textContent).toBe("🇳🇱");
      expect(svg.textContent!.match(/🇳🇱/gu)).toHaveLength(1);
      // Where flag emoji are missing the pair falls back to letters, painted in the text colour, not black.
      expect(svg.querySelector("[data-node] text")).toHaveAttribute("style", "fill: var(--t1);");
    }
    expect(container.querySelector("[data-stats]")).not.toHaveTextContent("🇳🇱");
  });

  it("the tunnel leg: brand with a halo when OK, amber when slow, rose dashed with a ✗ when the check fails, grey dashed when unused", () => {
    const { container, rerender } = render(<ConnectionPath {...PATH} />);
    const wide = () => drawing(container, "wide");
    const leg = () => wide().querySelector("path[data-leg]")!;
    expect(leg()).toHaveAttribute("data-leg", "ok");
    expect(leg()).not.toHaveAttribute("stroke-dasharray");
    expect(leg().getAttribute("style")).toMatch(/url\("?#path-wide-/);
    expect(leg().previousElementSibling).toHaveAttribute("stroke-width", "10");
    expect(within(columns(container)[2]!).getByText("42 ms")).toHaveClass("text-ok");
    expect(wide().querySelector("[data-pill='tunnel']")).toHaveTextContent("↓ 12.4 Mbit/s↑ 1.8 Mbit/s");
    expect(wide().querySelector("[data-node]")).toHaveAttribute("data-node", "ok");
    expect(wide().querySelector("[data-badge]")).toBeNull();

    rerender(<ConnectionPath {...PATH} node={SLOW} />);
    expect(leg()).toHaveAttribute("data-leg", "ok");
    expect(leg().getAttribute("style")).toContain("var(--warn)");
    expect(leg().previousElementSibling!.getAttribute("style")).toContain("var(--warn)");
    expect(within(columns(container)[2]!).getByText("831 ms")).toHaveTextContent(/^831 msslow$/);
    expect(within(columns(container)[2]!).getByText("831 ms")).toHaveClass("text-warn");

    rerender(<ConnectionPath {...PATH} node={BAD} />);
    expect(leg()).toHaveAttribute("data-leg", "bad");
    expect(leg()).toHaveAttribute("stroke-dasharray", "8 6");
    expect(leg().getAttribute("style")).toContain("var(--bad)");
    expect(wide().querySelector("[data-badge]")).not.toBeNull();
    expect(wide().querySelector("[data-pill='tunnel']")).toBeNull();
    expect(within(columns(container)[2]!).getByText("check failed")).toHaveClass("text-bad");
    expect(wide().querySelector("[data-node] [data-alarm]")).toHaveClass("motion-reduce:animate-none", "motion-reduce:opacity-45");

    rerender(<ConnectionPath {...PATH} node={STALE} />);
    expect(leg()).toHaveAttribute("data-leg", "off");
    expect(leg()).toHaveAttribute("stroke-dasharray", "6 6");
    expect(leg().getAttribute("style")).toContain("var(--t3)");
    expect(within(columns(container)[2]!).getByText("health stale")).toHaveClass("text-t2");
    expect(wide().querySelector("[data-node] [data-alarm]")).toBeNull();
  });

  it("direct traffic stays neutral at any rate, with its rates on the arc", () => {
    const { container, rerender } = render(<ConnectionPath {...PATH} />);
    const direct = () => drawing(container, "wide").querySelector("path[data-direct]")!;
    expect(direct()).toHaveAttribute("data-direct", "idle");
    expect(direct()).toHaveAttribute("stroke-dasharray", "4 5");
    expect(drawing(container, "wide").querySelector("[data-pill='direct']")).toHaveTextContent(/^idle$/);

    rerender(<ConnectionPath {...PATH} {...DIRECT_FLOWING} node={BAD} killSwitch={{ label: "OPEN", tone: "bad" }} />);
    expect(direct()).toHaveAttribute("data-direct", "flowing");
    expect(direct()).toHaveAttribute("stroke-dasharray", "5 5");
    expect(direct().getAttribute("style")).toBe("stroke: var(--t3);");
    const wide = drawing(container, "wide").querySelector("[data-pill='direct']")!;
    expect(wide.querySelectorAll("text")).toHaveLength(1);
    expect(wide).toHaveTextContent("↓ 4.5 Mbit/s ↑ 300 kbit/s");
    const narrow = drawing(container, "narrow").querySelector("[data-pill='direct']")!;
    expect([...narrow.querySelectorAll("text")].map((t) => t.textContent)).toEqual(["↓ 4.5 Mbit/s", "↑ 300 kbit/s"]);

    rerender(<ConnectionPath {...PATH} rates={null} tunnelLines={["—"]} directLines={["—"]} />);
    expect(direct()).toHaveAttribute("data-direct", "unknown");
  });

  it("packets run along a line only while its rate is above 0 and the frame is live, and stand still under reduced motion", () => {
    const { container, rerender } = render(<ConnectionPath {...PATH} />);
    const flows = (kind: string) => container.querySelectorAll(`[data-flow='${kind}']`);
    expect(flows("tunnel")).toHaveLength(2);
    expect(flows("direct")).toHaveLength(0);
    const tunnel = flows("tunnel")[0]!;
    expect(tunnel).toHaveClass("animate-[path-flow_0.9s_linear_infinite]", "motion-reduce:animate-none");

    rerender(<ConnectionPath {...PATH} {...DIRECT_FLOWING} />);
    expect(flows("tunnel")[0]).toBe(tunnel);   // the same element: a new rate does not restart the animation
    expect(flows("direct")).toHaveLength(2);
    expect(flows("direct")[0]).toHaveClass("animate-[path-flow_1.3s_linear_infinite]", "motion-reduce:animate-none");

    rerender(<ConnectionPath {...PATH} {...DIRECT_FLOWING} dim />);
    expect(container.querySelectorAll("[data-flow]")).toHaveLength(0);
    for (const pill of container.querySelectorAll("[data-pill]")) expect(pill).toHaveAttribute("data-dim", "true");
    expect(container.querySelector("[data-live]")).toHaveAttribute("data-dim", "true");

    rerender(<ConnectionPath {...PATH} rates={{ proxy: { down: 0, up: 0 }, direct: { down: 0, up: 0 } }} tunnelLines={["idle"]} />);
    expect(container.querySelectorAll("[data-flow]")).toHaveLength(0);
    rerender(<ConnectionPath {...PATH} rates={null} tunnelLines={["—"]} directLines={["—"]} />);
    expect(container.querySelectorAll("[data-flow]")).toHaveLength(0);
  });

  it("the gateway marker is the kill-switch: closed when ARMED, open with an alarm ring when OPEN, neutral when UNKNOWN", () => {
    const { container, rerender } = render(<ConnectionPath {...PATH} />);
    const lock = () => drawing(container, "wide").querySelector("[data-kill]")!;
    expect(lock()).toHaveAttribute("data-kill", "ARMED");
    expect(lock().querySelector("[data-alarm]")).toBeNull();

    rerender(<ConnectionPath {...PATH} killSwitch={{ label: "OPEN", tone: "bad" }} />);
    expect(lock()).toHaveAttribute("data-kill", "OPEN");
    expect(lock().querySelector("[data-alarm]")).toHaveClass("animate-[path-alarm_1.8s_ease-out_infinite]", "motion-reduce:animate-none", "motion-reduce:opacity-45");
    expect(within(columns(container)[1]!).getByText("OPEN")).toHaveClass("text-bad");
    // The ring grows past the lock: it is painted before both rate pills, so their opaque ground covers it.
    for (const svg of drawings(container)) {
      const kill = svg.querySelector("[data-kill] [data-alarm]")!;
      const pills = [...svg.querySelectorAll("[data-pill]")];
      expect(kill).not.toBeNull();
      expect(pills).toHaveLength(2);
      for (const pill of pills) expect(kill.compareDocumentPosition(pill) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }

    rerender(<ConnectionPath {...PATH} killSwitch={{ label: "UNKNOWN", tone: "neutral" }} />);
    expect(lock()).toHaveAttribute("data-kill", "UNKNOWN");
    expect(lock().querySelector("[data-alarm]")).toBeNull();
    expect(lock()).toHaveTextContent("?");
    expect(within(columns(container)[1]!).getByText("UNKNOWN")).toHaveClass("text-t2");
  });

  it("with nothing known every value is a dash, the node marker is an empty ring, and IPv6 off hides the v6 uplink", () => {
    const { container } = render(
      <ConnectionPath
        {...PATH}
        ipv6Enabled={false} clients={null} poolSize={null} gatewayIp={null} gatewayIface={null}
        nodeName="No node" nodeFlag="" node={{ state: "none", leg: "off", text: "—", note: null, tone: "neutral", ms: null }}
        egressIp={null} egressIp6={null} uplink={null} rates={null} tunnelLines={["—"]} directLines={["—"]}
        killSwitch={{ label: "UNKNOWN", tone: "neutral" }}
      />,
    );
    expect(columns(container).map((c) => c.textContent)).toEqual([
      "Devices—clientspool —", "Gateway——kill-switch UNKNOWN", "No node—connected —egress—", "Internetuplink v4 ?unknown",
    ]);
    const node = drawing(container, "wide").querySelector("[data-node]")!;
    expect(node).toHaveAttribute("data-node", "none");
    expect(node.children).toHaveLength(1);
    expect(drawing(container, "wide").querySelector("[data-leg]")).toHaveAttribute("data-leg", "off");
  });
});

describe("ConnectedFor", () => {
  it("counts coarsely on the shared ticker, never from a clock read while rendering, and says — without a start", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW_SEC * 1000));
    const since = NOW_SEC - (3 * 86_400 + 4 * 3_600);
    const view = (
      <>
        <p data-testid="on"><ConnectedFor since={since} /></p>
        <p data-testid="off"><ConnectedFor since={null} /></p>
      </>
    );
    const { rerender } = render(view);
    expect(screen.getByTestId("on")).toHaveTextContent(/^3d 4h$/);
    expect(screen.getByTestId("off")).toHaveTextContent(/^—$/);

    vi.setSystemTime(new Date((NOW_SEC + 3_600) * 1000));   // the clock moves, no tick yet
    rerender(view);
    expect(screen.getByTestId("on")).toHaveTextContent(/^3d 4h$/);

    act(() => { vi.advanceTimersByTime(15_000); });
    expect(screen.getByTestId("on")).toHaveTextContent(/^3d 5h$/);
  });
});

describe("EventFeed", () => {
  it("lists events in the order given, each with time, level and detail", () => {
    render(
      <EventFeed
        items={[
          { key: "a", time: "14:09:40", level: "bad", kind: "leak", detail: "untunneled traffic 120 kbit/s" },
          { key: "b", time: "14:02:11", level: "info", kind: "health", detail: "nl-ams-03 real check 45 ms" },
        ]}
      />,
    );
    const rows = within(screen.getByRole("list", { name: "Recent events" })).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual(["14:09:40badleak · untunneled traffic 120 kbit/s", "14:02:11infohealth · nl-ams-03 real check 45 ms"]);
    expect(rows[0]!.querySelector("[data-level]")).toHaveAttribute("data-level", "bad");
    expect(rows[1]!.querySelector("time")).toHaveTextContent("14:02:11");
  });

  it("says when there is nothing to show", () => {
    render(<EventFeed items={[]} empty="No failovers recorded." />);
    expect(screen.getByText("No failovers recorded.")).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });
});
