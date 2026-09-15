import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Status } from "../../api/client";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { STATUS, TUNNEL_PROFILES, holdConnectionWrite, mockApi, mockTunnel } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";

afterEach(() => act(() => settleConfirm(false)));

async function openAntiDpi(options: { phone?: boolean; status?: Partial<Status> } = {}) {
  if (options.phone) setViewportWidth(390);
  const api$ = mockTunnel(mockApi());
  api$.getStatus.mockResolvedValue({ ...STATUS, ...options.status });
  const view = renderApp("/tunnel/anti-dpi");
  const list = await screen.findByRole(options.phone ? "list" : "table", { name: "Profiles" });
  await waitFor(() => expect(api$.getStatus).toHaveBeenCalled());
  return { api$, list, ...view };
}

const row = (name: string) => document.querySelector<HTMLElement>(`[data-profile-id="${TUNNEL_PROFILES.find((p) => p.name === name)!.id}"]`)!;
const editor = () => screen.getByRole("region", { name: "Profile editor" });

async function answer(text: string, button: string) {
  const ask = await screen.findByRole("dialog", { name: "Confirm" });
  expect(ask).toHaveTextContent(text);
  await userEvent.click(within(ask).getByRole("button", { name: button }));
}

describe("Anti-DPI › profiles table (T1)", () => {
  it("header, hint and one row per profile: badges, used by, fingerprint, features and QUIC", async () => {
    const { list } = await openAntiDpi();
    const section = screen.getByRole("region", { name: "Anti-DPI profiles" });
    expect(within(section).getByRole("heading", { name: "3 profiles" })).toBeInTheDocument();
    expect(section).toHaveTextContent("· ⟳ live-apply");
    expect(section).toHaveTextContent("Evasion profiles. Assign one per node from its Edit on the Nodes tab. The profile governing the live tunnel right now is marked ● active.");
    expect(within(list).getAllByRole("row").slice(1).map((tr) => tr.getAttribute("data-profile-id"))).toEqual(["1", "2", "3"]);
    expect(row("balanced")).toHaveTextContent("balanceddefault8chrome");
    expect(within(row("balanced")).queryByText("● active")).toBeNull();
    expect(row("fragment-tls")).toHaveTextContent("fragment-tls● active3chrome");
    expect(row("mux-heavy")).toHaveTextContent("mux-heavy—firefox");
    expect(within(row("fragment-tls")).getByText("frag on")).toBeInTheDocument();
    expect(within(row("fragment-tls")).getByText("noise on")).toBeInTheDocument();
    expect(within(row("fragment-tls")).getByText("mux off")).toBeInTheDocument();
    expect(within(row("mux-heavy")).getByText("DoH off")).toBeInTheDocument();
    expect(within(row("mux-heavy")).getAllByRole("cell").at(-2)).toHaveTextContent("proxy");
  });

  it("every row can be edited, cloned and applied; the default has no Make default or Delete", async () => {
    await openAntiDpi();
    const balanced = row("balanced");
    for (const name of ["Edit balanced", "Clone balanced", "Apply balanced to the active node"]) expect(within(balanced).getByRole("button", { name })).toBeEnabled();
    expect(within(balanced).queryByRole("button", { name: "Make balanced the default" })).toBeNull();
    expect(within(balanced).queryByRole("button", { name: "Delete balanced" })).toBeNull();
    expect(within(row("mux-heavy")).getByRole("button", { name: "Make mux-heavy the default" })).toBeEnabled();
    expect(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" })).toHaveAttribute("title", "Apply to the active node and re-apply now");
  });

  it("loading is a skeleton, a failed load an error with Retry, no profiles says so", async () => {
    const api$ = mockApi();
    api$.listProfiles.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/tunnel/anti-dpi");
    expect(await screen.findByRole("region", { name: "Profiles" })).toHaveAttribute("aria-busy", "true");
    const error = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(error).toHaveTextContent("Profiles did not load");
    api$.listProfiles.mockResolvedValue([]);
    await userEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No profiles")).toBeInTheDocument();
  });
});

describe("Anti-DPI › row actions (T2)", () => {
  it("Apply to active asks, then assigns the profile to the active node as a connection write", async () => {
    const { api$ } = await openAntiDpi();
    const success = vi.spyOn(toast, "success");
    let finish: () => void = () => {};
    api$.applyProfileActive.mockImplementation(() => new Promise((resolve) => { finish = () => resolve({ ok: true, node_id: 1 }); }));
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" }));
    await answer("Apply mux-heavy to nl-ams-03 and re-apply the tunnel now? Devices may drop briefly.", "Apply");
    await waitFor(() => expect(api$.applyProfileActive).toHaveBeenCalledWith(3));
    // one profile write at a time: every row action waits
    await waitFor(() => expect(within(row("balanced")).getByRole("button", { name: "Edit balanced" })).toBeDisabled());
    expect(within(row("fragment-tls")).getByRole("button", { name: "Delete fragment-tls" })).toBeDisabled();
    await act(async () => finish());
    await waitFor(() => expect(success).toHaveBeenCalledWith("applied to active node 1", { duration: 8000 }));
    await waitFor(() => expect(within(row("balanced")).getByRole("button", { name: "Edit balanced" })).toBeEnabled());
  });

  it("Apply is not sent when another connection change started while the question was open; a 409 is no active node", async () => {
    const { api$, client } = await openAntiDpi();
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" }));
    await screen.findByRole("dialog", { name: "Confirm" });
    const release = holdConnectionWrite(client);
    await answer("Apply mux-heavy", "Apply");
    expect(error).toHaveBeenCalledWith("Another connection change is still running — try again when it finishes", { duration: 20000 });
    expect(api$.applyProfileActive).not.toHaveBeenCalled();
    expect(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" })).toBeDisabled();
    expect(within(row("mux-heavy")).getByRole("button", { name: "Edit mux-heavy" })).toBeEnabled();
    await release();

    api$.applyProfileActive.mockRejectedValue(new ApiError(409, "no active node"));
    await waitFor(() => expect(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" })).toBeEnabled());
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" }));
    await answer("Apply mux-heavy", "Apply");
    await waitFor(() => expect(error).toHaveBeenCalledWith("No active node", { duration: 20000 }));
  });

  it("Apply is not sent to a node the question did not name: a failover while it was open, or no active node any more", async () => {
    const { api$, client } = await openAntiDpi();
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" }));
    await screen.findByRole("dialog", { name: "Confirm" });
    // Auto-failover moves the tunnel to another node while the question still names nl-ams-03.
    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: 2 });
    act(() => { client.setQueryData<Status>(keys.status, (old) => ({ ...old!, active_node_id: 2 })); });
    await answer("Apply mux-heavy to nl-ams-03", "Apply");
    expect(error).toHaveBeenCalledWith("the active node changed — ask again", { duration: 20000 });
    expect(api$.applyProfileActive).not.toHaveBeenCalled();

    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" }));
    await screen.findByRole("dialog", { name: "Confirm" });
    api$.getStatus.mockResolvedValue({ ...STATUS, active_node_id: null });
    act(() => { client.setQueryData<Status>(keys.status, (old) => ({ ...old!, active_node_id: null })); });
    await answer("Apply mux-heavy", "Apply");
    expect(error).toHaveBeenCalledWith("No active node", { duration: 20000 });
    expect(api$.applyProfileActive).not.toHaveBeenCalled();
  });

  it("with no active node, ⚡ is off and says why", async () => {
    await openAntiDpi({ status: { active_node_id: null } });
    await waitFor(() => expect(within(row("mux-heavy")).getByRole("button", { name: "Apply mux-heavy to the active node" })).toBeDisabled());
    expect(screen.getByText("⚡ No active node — connect one to apply a profile live.")).toBeInTheDocument();
  });

  it("Make default is a connection write with its message", async () => {
    const { api$ } = await openAntiDpi();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Make mux-heavy the default" }));
    await waitFor(() => expect(api$.setDefaultProfile).toHaveBeenCalledWith(3));
    await waitFor(() => expect(success).toHaveBeenCalledWith("default updated", { duration: 8000 }));
    await waitFor(() => expect(api$.listProfiles).toHaveBeenCalledTimes(2));
  });

  it("Delete asks with the node count; Cancel keeps the profile, Delete removes it; a refusal is shown", async () => {
    const { api$ } = await openAntiDpi();
    const error = vi.spyOn(toast, "error");
    await userEvent.click(within(row("fragment-tls")).getByRole("button", { name: "Delete fragment-tls" }));
    expect((await screen.findByRole("dialog", { name: "Confirm" })).querySelector("p")!.textContent).toBe('Delete profile "fragment-tls"?\n3 node(s) using it fall back to the default.');
    await answer("fragment-tls", "Cancel");
    expect(api$.deleteProfile).not.toHaveBeenCalled();
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Delete mux-heavy" }));
    const ask = await screen.findByRole("dialog", { name: "Confirm" });
    expect(ask.querySelector("p")!.textContent).toBe('Delete profile "mux-heavy"?');
    await userEvent.click(within(ask).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api$.deleteProfile).toHaveBeenCalledWith(3));
    api$.deleteProfile.mockRejectedValue(new ApiError(409, "cannot delete the default profile"));
    await userEvent.click(within(row("fragment-tls")).getByRole("button", { name: "Delete fragment-tls" }));
    await answer("fragment-tls", "Delete");
    await waitFor(() => expect(error).toHaveBeenCalledWith("cannot delete the default profile", { duration: 20000 }));
  });

  it("Edit loads a profile into the editor; deleting that profile clears it; New starts blank", async () => {
    await openAntiDpi();
    expect(within(editor()).getByRole("heading", { name: "New profile" })).toBeInTheDocument();
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Edit mux-heavy" }));
    expect(within(editor()).getByRole("heading", { name: "Editing profile · id 3" })).toBeInTheDocument();
    expect(editor()).toHaveTextContent("mux-heavyused by 0 nodes");
    expect(row("mux-heavy")).toHaveAttribute("data-editing", "true");
    await userEvent.click(within(row("mux-heavy")).getByRole("button", { name: "Delete mux-heavy" }));
    await answer('Delete profile "mux-heavy"?', "Delete");
    await waitFor(() => expect(within(editor()).getByRole("heading", { name: "New profile" })).toBeInTheDocument());

    await userEvent.click(within(row("fragment-tls")).getByRole("button", { name: "Edit fragment-tls" }));
    expect(editor()).toHaveTextContent("fragment-tls● activeused by 3 nodes");
    await userEvent.click(within(editor()).getByRole("button", { name: "New" }));
    expect(within(editor()).getByRole("heading", { name: "New profile" })).toBeInTheDocument();
    await userEvent.click(within(row("balanced")).getByRole("button", { name: "Clone balanced" }));
    expect(within(editor()).getByRole("heading", { name: "New profile" })).toBeInTheDocument();
    expect(row("balanced")).not.toHaveAttribute("data-editing");
  });
});

describe("Anti-DPI on a phone (T1, T2)", () => {
  it("profile cards with badges, used by, features, ⚡ Apply and a ⋯ menu; the default's menu has no Make default or Delete", async () => {
    const { list } = await openAntiDpi({ phone: true });
    const cards = within(list).getAllByRole("listitem");
    expect(cards).toHaveLength(3);
    const fragment = within(list).getByRole("listitem", { name: "fragment-tls" });
    expect(fragment).toHaveTextContent("fragment-tls● activeused by 3 · chrome");
    expect(fragment).toHaveTextContent("QUIC drop");
    expect(within(fragment).getByRole("button", { name: "Apply fragment-tls to the active node" })).toBeEnabled();
    await userEvent.click(within(list).getByRole("button", { name: "More actions for balanced" }));
    expect((await screen.findAllByRole("menuitem")).map((item) => item.textContent)).toEqual(["Edit", "Clone"]);
    await userEvent.keyboard("{Escape}");
    await userEvent.click(within(list).getByRole("button", { name: "More actions for mux-heavy" }));
    expect((await screen.findAllByRole("menuitem")).map((item) => item.textContent)).toEqual(["Edit", "Clone", "Make default", "Delete…"]);
  });

  it("Edit from the menu opens the editor as a page; Back returns to the list", async () => {
    await openAntiDpi({ phone: true });
    expect(screen.queryByRole("region", { name: "Profile editor" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "More actions for mux-heavy" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
    const page = await screen.findByRole("region", { name: "Profile editor" });
    expect(within(page).getByRole("heading", { name: "Editing profile · id 3" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Profiles" })).toBeNull();
    await userEvent.click(within(editor()).getByRole("button", { name: "Back to profiles" }));
    expect(await screen.findByRole("list", { name: "Profiles" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "New profile" }));
    expect(within(editor()).getByRole("heading", { name: "New profile" })).toBeInTheDocument();
  });
});
