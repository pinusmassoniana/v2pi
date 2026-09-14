import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ConnectionPath, type ConnectionPathProps } from "./ConnectionPath";
import { EventFeed } from "./EventFeed";

const PATH: ConnectionPathProps = {
  clients: 12, poolSize: 50, gatewayIp: "10.0.2.1", gatewayIface: "eth0.2",
  nodeName: "nl-ams-03", nodeFlag: "🇳🇱", latencyMs: 42, egressIp: "185.107.56.21", egressIp6: "2a0b:4d07::21",
  uplink: true, uplink6: true, ipv6Enabled: true, leg: "ok", bypassBps: 0, killSwitch: { label: "ARMED", tone: "ok" },
};

describe("ConnectionPath", () => {
  it("names the four stops and the legs' state for assistive tech", () => {
    render(<ConnectionPath {...PATH} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(
      "Connection path: devices, gateway, nl-ams-03, internet. Tunnel leg OK; bypass idle.",
    );
    expect(screen.getByText("🇳🇱 nl-ams-03")).toBeInTheDocument();
  });

  it("details each stop: clients and pool, gateway address, latency and egress, uplinks", () => {
    const { container } = render(<ConnectionPath {...PATH} />);
    const details = container.querySelector("svg + div")!;
    expect([...details.children].map((c) => c.textContent)).toEqual([
      "Devices · 12 clientspool 50",
      "Gateway10.0.2.1 · eth0.2",
      "Node · 42 ms · egress185.107.56.212a0b:4d07::21",
      "Internet · uplinkv4 ✓ · v6 ✓",
    ]);
  });

  it("a healthy leg is solid brand, a failing one rose, an unused one dashed", () => {
    const { container, rerender } = render(<ConnectionPath {...PATH} />);
    const leg = () => container.querySelector("path[data-leg]")!;
    expect(leg()).toHaveAttribute("data-leg", "ok");
    expect(leg()).not.toHaveAttribute("stroke-dasharray");
    expect(screen.getByText("OK").parentElement).toHaveAttribute("data-tone", "ok");

    rerender(<ConnectionPath {...PATH} leg="bad" latencyMs={null} />);
    expect(leg().getAttribute("style")).toContain("var(--bad)");
    expect(screen.getByText("DOWN").parentElement).toHaveAttribute("data-tone", "bad");
    expect(screen.getByText("Node · egress")).toBeInTheDocument();

    rerender(<ConnectionPath {...PATH} leg="off" />);
    expect(leg()).toHaveAttribute("stroke-dasharray", "6 6");
    expect(screen.getByText("OFF").parentElement).toHaveAttribute("data-tone", "neutral");
  });

  it("the bypass line turns amber with any direct traffic and names the rate", () => {
    const { container, rerender } = render(<ConnectionPath {...PATH} />);
    expect(container.querySelector("path[data-bypass]")).toHaveAttribute("data-bypass", "idle");
    expect(screen.getByText("idle").parentElement).toHaveAttribute("data-tone", "neutral");
    rerender(<ConnectionPath {...PATH} bypassBps={1_200} />);
    expect(container.querySelector("path[data-bypass]")).toHaveAttribute("data-bypass", "leaking");
    expect(screen.getByText("bypass · direct 1 kbit/s")).toBeInTheDocument();
    expect(within(container.querySelector("svg + div + div") as HTMLElement).getByText("1 kbit/s").parentElement).toHaveAttribute("data-tone", "warn");
  });

  it("hides the v6 uplink without IPv6, and copes with nothing known yet", () => {
    const { container } = render(
      <ConnectionPath {...PATH} ipv6Enabled={false} clients={null} poolSize={null} gatewayIp={null} gatewayIface={null}
        nodeName={null} nodeFlag="" latencyMs={null} egressIp={null} egressIp6={null} uplink={null} leg="off" killSwitch={{ label: "UNKNOWN", tone: "neutral" }} />,
    );
    expect([...container.querySelector("svg + div")!.children].map((c) => c.textContent)).toEqual([
      "Devices · — clientspool —", "Gateway—", "Node · egress—", "Internet · uplinkv4 ?",
    ]);
    expect(screen.getByText("No node")).toBeInTheDocument();
    expect(screen.getByText("UNKNOWN").parentElement).toHaveAttribute("data-tone", "neutral");
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
