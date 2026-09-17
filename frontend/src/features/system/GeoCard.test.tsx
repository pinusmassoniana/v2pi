import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { settleConfirm } from "../../components/confirm";
import { GEO, mockApi, mockSystem } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { geoOutcome } from "./GeoCard";

afterEach(() => act(() => { settleConfirm(false); toast.dismiss(); }));

async function openPanel() {
  const api$ = mockSystem(mockApi());
  const view = renderApp("/system/panel");
  await screen.findByRole("region", { name: "Geo data" });
  await screen.findByText("Stock lists");        // the card's own read has landed
  return { api$, ...view };
}

const card = () => screen.getByRole("region", { name: "Geo data" });

async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

describe("System › Panel — geo data (A3)", () => {
  it("shows both datasets with their size, age and where they live", async () => {
    await openPanel();
    const geo = card();
    expect(within(geo).getByText("Stock lists")).toBeInTheDocument();
    expect(within(geo).getByText("RU lists")).toBeInTheDocument();
    // 18.4 MB + 73.7 MB of RU data, updated an hour ago; the stock pair is the image's, 90 days old.
    expect(geo).toHaveTextContent("updated 1h ago");
    expect(geo).toHaveTextContent(GEO.asset_dir);
    expect(geo).toHaveTextContent("11.40 GB free");
  });

  it("an update asks first, says the tunnel restarts, and reports what changed", async () => {
    const { api$ } = await openPanel();

    await userEvent.click(within(card()).getByRole("button", { name: "Update ru lists" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent("devices drop briefly");
    await userEvent.click(within(dialog).getByRole("button", { name: "Update" }));

    await waitFor(() => expect(api$.updateGeo).toHaveBeenCalledWith("ru"));
    expect(await screen.findByText(/ru geo data updated/)).toBeInTheDocument();
  });

  it("declining the question replaces nothing", async () => {
    const { api$ } = await openPanel();
    await userEvent.click(within(card()).getByRole("button", { name: "Update stock lists" }));
    await answer("Cancel");
    expect(api$.updateGeo).not.toHaveBeenCalled();
  });

  it("a refused update is reported with the gateway's reason, not as a success", async () => {
    const { api$ } = await openPanel();
    api$.updateGeo.mockResolvedValueOnce({
      ok: false, dataset: "ru", files: [], reloaded: false, geo: GEO,
      error: "the category 'ru-blocked' is not in geosite_ru.dat. Update that geo data on System › Panel, "
        + "or pick a category the installed data carries.",
    });
    const error = vi.spyOn(toast, "error");

    await userEvent.click(within(card()).getByRole("button", { name: "Update ru lists" }));
    await answer("Update");

    await waitFor(() => expect(error).toHaveBeenCalledWith(
      expect.stringContaining("is not in geosite_ru.dat"), expect.anything()));
  });

  it("Revert shows only where a previous copy exists, and asks too", async () => {
    const { api$ } = await openPanel();
    const geo = card();
    const ru = geo.querySelector('[data-geo-dataset="ru"]') as HTMLElement;
    const stock = geo.querySelector('[data-geo-dataset="stock"]') as HTMLElement;
    expect(within(stock).queryByRole("button", { name: "Revert" })).toBeNull();

    await userEvent.click(within(ru).getByRole("button", { name: "Revert" }));
    await answer("Revert");
    await waitFor(() => expect(api$.revertGeo).toHaveBeenCalledWith("ru"));
  });

  it("a transport failure is an error, not a silent no-op", async () => {
    const { api$ } = await openPanel();
    api$.updateGeo.mockRejectedValueOnce(new ApiError(502, "gateway is busy"));
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(card()).getByRole("button", { name: "Update ru lists" }));
    await answer("Update");
    await waitFor(() => expect(error).toHaveBeenCalled());
  });
});

describe("geoOutcome", () => {
  it("names the files and the restart, or the reason nothing changed", () => {
    expect(geoOutcome({ ok: true, dataset: "ru", files: ["geoip_ru.dat"], reloaded: true, error: "", geo: GEO }))
      .toBe("ru geo data updated (geoip_ru.dat) · tunnel restarted");
    expect(geoOutcome({ ok: true, dataset: "stock", files: [], reloaded: false, error: "", geo: GEO }))
      .toBe("stock geo data updated (nothing)");
    expect(geoOutcome({ ok: false, dataset: "ru", files: [], reloaded: false, error: "checksum mismatch", geo: GEO }))
      .toBe("checksum mismatch");
  });
});
