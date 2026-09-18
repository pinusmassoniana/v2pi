import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { DIAGNOSIS, mockApi, mockNodeGroups } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";
import { msText, phaseRows, rateText, transferText } from "./diagnose";

async function openNode() {
  setViewportWidth(390);
  const api$ = mockNodeGroups(mockApi());
  const view = renderApp("/nodes/1");
  await screen.findByRole("region", { name: "Diagnose" });
  return { api$, ...view };
}

const card = () => screen.getByRole("region", { name: "Diagnose" });

describe("Node detail › Diagnose (B3)", () => {
  it("measures only when asked, then shows the phases and what they mean", async () => {
    const { api$ } = await openNode();
    expect(api$.diagnoseNode).not.toHaveBeenCalled();     // never on mount, never on a timer
    expect(card()).toHaveTextContent("Nothing is scheduled");

    await userEvent.click(within(card()).getByRole("button", { name: "Diagnose" }));

    await waitFor(() => expect(api$.diagnoseNode).toHaveBeenCalledWith(1));
    expect(await within(card()).findByText("stalls mid-stream")).toBeInTheDocument();
    expect(card()).toHaveTextContent("stopped after 96 KB");
    expect(card()).toHaveTextContent("24 ms");            // TCP
    expect(card()).toHaveTextContent("71 ms");            // handshake
    expect(card()).toHaveTextContent("96 KB of 256 KB");
    // The card must never claim proof of throttling.
    expect(card()).toHaveTextContent("A heuristic, not proof");
    expect(within(card()).getByRole("button", { name: "Run again" })).toBeInTheDocument();
  });

  it("a second diagnosis running elsewhere is said, not queued", async () => {
    const { api$ } = await openNode();
    api$.diagnoseNode.mockRejectedValue(new ApiError(409, "a diagnosis is already running"));

    await userEvent.click(within(card()).getByRole("button", { name: "Diagnose" }));

    expect(await screen.findByText("a diagnosis is already running")).toBeInTheDocument();
  });
});

describe("diagnosis helpers", () => {
  it("never invents a number for a phase that did not run", () => {
    expect(msText(null)).toBe("—");
    expect(msText(0)).toBe("0 ms");
    expect(rateText(null)).toBe("—");
    expect(rateText(0)).toBe("—");
    expect(rateText(191)).toBe("191 kbit/s");
    expect(rateText(4_200)).toBe("4.2 Mbit/s");
    expect(transferText({ bytes: 98_304, requested_bytes: 262_144 })).toBe("96 KB of 256 KB");
  });

  it("lists the phases in the order they happen, and says when there is no TLS to time", () => {
    expect(phaseRows(DIAGNOSIS).map((row) => row.key)).toEqual(["TCP", "HANDSHAKE", "FIRST BYTE", "TRANSFER"]);
    const shadowsocks = phaseRows({ ...DIAGNOSIS, tls_ms: null });
    expect(shadowsocks[1]).toMatchObject({ value: "—", sub: "no TLS layer" });
  });
});
