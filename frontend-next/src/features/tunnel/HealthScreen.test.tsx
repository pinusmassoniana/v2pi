import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Settings, type Status } from "../../api/client";
import { SETTINGS_WRITE } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { FAILOVER_STATUS, SETTINGS, STATUS, mockApi } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";

afterEach(() => act(() => settleConfirm(false)));

async function openHealth(options: { status?: Status; settings?: Partial<Settings>; phone?: boolean } = {}) {
  if (options.phone) setViewportWidth(390);
  const api$ = mockApi();
  api$.getStatus.mockResolvedValue(options.status ?? FAILOVER_STATUS);
  api$.getSettings.mockResolvedValue({ ...SETTINGS, ...options.settings });
  const view = renderApp("/tunnel/health");
  await screen.findByRole("region", { name: "Health monitoring" });
  return { api$, ...view };
}

const monitoring = () => screen.getByRole("region", { name: "Health monitoring" });
const failover = () => screen.getByRole("region", { name: "Auto-failover" });
const strip = () => screen.getByRole("region", { name: "Current state" });
const saveButton = () => screen.getByRole("button", { name: "Save" });

describe("Health & failover › state strip", () => {
  it("reads health, failover readiness, failovers in 24 h and the last switch from the status poll", async () => {
    await openHealth();
    await waitFor(() => expect(strip()).toHaveTextContent("Health checksOn"));
    expect(strip()).toHaveTextContent("Auto-failoverFailover ready4 eligible standby");
    expect(strip()).toHaveTextContent("Failovers · 24 h1");
    expect(strip()).toHaveTextContent("Last switch12 min ago");
  });

  it("checks off, no standby, never switched", async () => {
    await openHealth({ status: { ...STATUS, health_enabled: false, failover_ready: false, eligible_standby_count: 0, failovers_24h: 0, last_failover_at: null } });
    await waitFor(() => expect(strip()).toHaveTextContent("Health checksOff"));
    expect(strip()).toHaveTextContent("Auto-failoverNo eligible standby0 eligible standby");
    expect(strip()).toHaveTextContent("Last switchnever");
  });

  it("failover switched off reads Off; an unreachable gateway reads unknown", async () => {
    const { api$, client } = await openHealth({ status: { ...FAILOVER_STATUS, failover_enabled: false } });
    await waitFor(() => expect(strip()).toHaveTextContent("Auto-failoverOff"));
    api$.getStatus.mockRejectedValue(new ApiError(0, "network error"));
    await act(() => client.refetchQueries({ queryKey: keys.status }));
    await waitFor(() => expect(strip()).toHaveTextContent("Health checksunknownAuto-failoverunknownFailovers · 24 hunknownLast switchunknown"));
  });
});

describe("Health & failover › form (G3, G4)", () => {
  it("shows the stored settings with the contract's words; Save is off until something changed", async () => {
    await openHealth();
    expect(within(monitoring()).getByRole("switch", { name: "Health checks" })).toHaveAttribute("aria-checked", "true");
    expect(monitoring()).toHaveTextContent("master switch — off stops both checks below");
    expect(monitoring()).toHaveTextContent("TCP + direct HTTPS across the whole pool");
    expect(within(monitoring()).getByLabelText("Server sweep interval")).toHaveValue(10);
    expect(within(monitoring()).getByLabelText("Active-server check interval")).toHaveValue(30);
    expect(within(monitoring()).getByLabelText("Probe URL")).toHaveValue("https://www.gstatic.com/generate_204");
    expect(monitoring()).toHaveTextContent("A real request through the connected server, via a throwaway xray so your live connection is untouched. Auto-failover reads this check, not the sweep — so you can turn the sweep off and keep failover working.");
    expect(within(failover()).getByLabelText("Hysteresis")).toHaveValue(3);
    expect(failover()).toHaveTextContent("consecutive failed real checks before switching");
    expect(within(failover()).getByLabelText("Cooldown")).toHaveValue(300);
    expect(failover()).toHaveTextContent("minimum time between automatic switches");
    expect(failover()).toHaveTextContent("A node that was just failed away from isn't picked again for 10 minutes.");
    expect(screen.getByText("Health and failover changes don't re-apply the tunnel.")).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("floors show under their fields and keep Save off", async () => {
    const { api$ } = await openHealth();
    const active = within(monitoring()).getByLabelText("Active-server check interval");
    await userEvent.clear(active);
    await userEvent.type(active, "5");
    expect(active).toHaveAccessibleDescription("Active-server check interval must be ≥ 10 s");
    expect(saveButton()).toBeDisabled();
    await userEvent.clear(within(monitoring()).getByLabelText("Server sweep interval"));
    await userEvent.type(within(monitoring()).getByLabelText("Server sweep interval"), "0");
    expect(await within(monitoring()).findByText("Server sweep interval must be ≥ 1 min")).toBeInTheDocument();
    await userEvent.clear(within(failover()).getByLabelText("Hysteresis"));
    await userEvent.type(within(failover()).getByLabelText("Hysteresis"), "0");
    expect(await within(failover()).findByText("Hysteresis must be ≥ 1")).toBeInTheDocument();
    await userEvent.clear(within(failover()).getByLabelText("Cooldown"));
    expect(await within(failover()).findByText("Cooldown must be ≥ 0")).toBeInTheDocument();
    expect(api$.putSettings).not.toHaveBeenCalled();
  });

  it("saves only the settings that changed, in stored units, then reads as saved", async () => {
    const { api$ } = await openHealth();
    const success = vi.spyOn(toast, "success");
    await userEvent.clear(within(monitoring()).getByLabelText("Server sweep interval"));
    await userEvent.type(within(monitoring()).getByLabelText("Server sweep interval"), "30");
    await userEvent.clear(within(failover()).getByLabelText("Hysteresis"));
    await userEvent.type(within(failover()).getByLabelText("Hysteresis"), "4");
    expect(screen.getByText("● unsaved changes ·")).toBeInTheDocument();
    await userEvent.click(saveButton());
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ health_interval: 1800, health_hysteresis: 4 }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Health settings saved", { duration: 8000 }));
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(within(monitoring()).getByLabelText("Server sweep interval")).toHaveValue(30);
    expect(screen.queryByText("● unsaved changes ·")).toBeNull();
  });

  it("with the master switch off the sweep is off-limits but keeps its values, and failover says it can never fire", async () => {
    const { api$ } = await openHealth();
    await userEvent.click(within(monitoring()).getByRole("switch", { name: "Health checks" }));
    expect(within(monitoring()).getByRole("switch", { name: "Server sweep" })).toBeDisabled();
    expect(within(monitoring()).getByLabelText("Server sweep interval")).toBeDisabled();
    expect(within(monitoring()).getByLabelText("Server sweep interval")).toHaveValue(10);
    expect(within(monitoring()).getByLabelText("Active-server check interval")).toBeDisabled();
    expect(failover()).toHaveTextContent("health checks are off — failover can never fire");
    await userEvent.click(saveButton());
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ health_enabled: false }));
  });

  it("the sweep interval is off while the sweep is", async () => {
    await openHealth({ settings: { health_sweep_enabled: false } });
    expect(within(monitoring()).getByLabelText("Server sweep interval")).toBeDisabled();
    expect(within(monitoring()).getByLabelText("Active-server check interval")).toBeEnabled();
  });

  it("Save waits for any other settings write, and a failed save re-reads the settings and keeps the edits", async () => {
    const { api$, client } = await openHealth();
    const error = vi.spyOn(toast, "error");
    await userEvent.clear(within(failover()).getByLabelText("Cooldown"));
    await userEvent.type(within(failover()).getByLabelText("Cooldown"), "60");
    let release: () => void = () => {};
    const running = client.getMutationCache().build(client, { mutationKey: SETTINGS_WRITE, mutationFn: () => new Promise<void>((resolve) => { release = resolve; }) }).execute(undefined);
    await waitFor(() => expect(saveButton()).toBeDisabled());
    await act(async () => { release(); await running; });
    await waitFor(() => expect(saveButton()).toBeEnabled());

    api$.putSettings.mockRejectedValue(new ApiError(422, "failover_cooldown must be an integer"));
    const reads = api$.getSettings.mock.calls.length;
    await userEvent.click(saveButton());
    await waitFor(() => expect(error).toHaveBeenCalledWith("failover_cooldown must be an integer", { duration: 20000 }));
    await waitFor(() => expect(api$.getSettings.mock.calls.length).toBeGreaterThan(reads));
    expect(within(failover()).getByLabelText("Cooldown")).toHaveValue(60);
  });

  it("follows the gateway's settings where nothing was typed, and never sends back what it did not change", async () => {
    const { api$, client } = await openHealth();
    await userEvent.clear(within(failover()).getByLabelText("Cooldown"));
    await userEvent.type(within(failover()).getByLabelText("Cooldown"), "60");
    api$.getSettings.mockResolvedValue({ ...SETTINGS, health_hysteresis: 5 });
    await act(() => client.refetchQueries({ queryKey: keys.settings }));
    await waitFor(() => expect(within(failover()).getByLabelText("Hysteresis")).toHaveValue(5));
    expect(within(failover()).getByLabelText("Cooldown")).toHaveValue(60);
    await userEvent.click(saveButton());
    await waitFor(() => expect(api$.putSettings).toHaveBeenCalledWith({ failover_cooldown: 60 }));
  });

  it("leaving with unsaved settings asks first", async () => {
    const { router } = await openHealth();
    await userEvent.click(within(failover()).getByRole("switch", { name: "Auto-failover" }));
    act(() => void router.navigate({ to: "/tunnel/routing" }));
    const ask = await screen.findByRole("dialog", { name: "Confirm" });
    expect(ask).toHaveTextContent("Discard unsaved changes and leave this screen?");
    await userEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(router.state.location.pathname).toBe("/tunnel/health");
  });

  it("loading is a skeleton; a failed load is an error with Retry", async () => {
    const api$ = mockApi();
    api$.getSettings.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/tunnel/health");
    await screen.findByRole("region", { name: "Current state" });
    expect(document.querySelector("[aria-busy]")).not.toBeNull();
    expect(screen.queryByRole("region", { name: "Health monitoring" })).toBeNull();
    const error = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(error).toHaveTextContent("Health settings did not load");
    api$.getSettings.mockResolvedValue(SETTINGS);
    await userEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Health monitoring" })).toBeInTheDocument();
  });

  it("on a phone the cards stack and Save sticks above the tab bar", async () => {
    await openHealth({ phone: true });
    expect(saveButton().parentElement).toHaveClass("max-md:sticky");
  });
});
