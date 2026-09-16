import { QueryClient } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { Status } from "../api/client";
import { keys } from "../api/keys";
import { settleConfirm } from "../components/confirm";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { STATUS } from "../test/fixtures";
import { START_TUNNEL_CONFIRM, confirmTunnelStart, knownStatus, startsStoppedTunnel } from "./tunnelStart";

afterEach(() => act(() => settleConfirm(false)));

/** A client whose status query holds `status`, as a landed poll would. */
function withStatus(status: Status): QueryClient {
  const client = new QueryClient();
  client.setQueryData(keys.status, status);
  return client;
}

/** Let the status query's next poll fail, leaving whatever it held before in the cache. */
async function pollFails(client: QueryClient): Promise<void> {
  await client
    .fetchQuery({ queryKey: keys.status, queryFn: () => Promise.reject(new Error("unreachable")), retry: false })
    .catch(() => {});
}

const stopped = (activeNodeId: number | null): Status => ({ ...STATUS, running: false, xray_state: "stopped", active_node_id: activeNodeId });

describe("knownStatus", () => {
  it("is the status the poll last landed, and nothing at all when that is not known", async () => {
    expect(knownStatus(new QueryClient())).toBeUndefined();   // never loaded

    const client = withStatus(STATUS);
    expect(knownStatus(client)).toEqual(STATUS);

    await pollFails(client);
    expect(client.getQueryData(keys.status)).toEqual(STATUS);   // the last reply is still cached …
    expect(knownStatus(client)).toBeUndefined();                // … but it is no longer known to hold
  });
});

describe("startsStoppedTunnel", () => {
  it("is true when xray was stopped on purpose while a node is still selected: the re-apply brings it back up", () => {
    expect(startsStoppedTunnel(withStatus(stopped(1)))).toBe(true);
  });

  it("is false with no active node, stopped or not: there is nothing to re-apply, so nothing starts", () => {
    expect(startsStoppedTunnel(withStatus(stopped(null)))).toBe(false);
    expect(startsStoppedTunnel(withStatus({ ...STATUS, active_node_id: null }))).toBe(false);
  });

  it("is false while the tunnel runs", () => {
    expect(startsStoppedTunnel(withStatus(STATUS))).toBe(false);
  });

  it("is true whenever the status is not known — never loaded, or its last poll failed — so the question is asked, not skipped", async () => {
    expect(startsStoppedTunnel(new QueryClient())).toBe(true);

    const client = withStatus(STATUS);   // running, and it would not ask
    await pollFails(client);
    expect(startsStoppedTunnel(client)).toBe(true);
  });
});

describe("confirmTunnelStart", () => {
  it("asks before starting a stopped tunnel, and passes the answer back", async () => {
    render(<ConfirmDialog />);
    const client = withStatus(stopped(1));

    const cancelled = confirmTunnelStart(client);
    let dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent(START_TUNNEL_CONFIRM);
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await expect(cancelled).resolves.toBe(false);

    const accepted = confirmTunnelStart(client);
    dialog = await screen.findByRole("dialog", { name: "Confirm" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await expect(accepted).resolves.toBe(true);
  });

  it("asks nothing when the write would start nothing", async () => {
    render(<ConfirmDialog />);
    await expect(confirmTunnelStart(withStatus(STATUS))).resolves.toBe(true);
    await expect(confirmTunnelStart(withStatus(stopped(null)))).resolves.toBe(true);
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
  });
});
