import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type TuningProfile } from "../../api/client";
import { CONNECTION_WRITE } from "../../api/invalidation";
import { settleConfirm } from "../../components/confirm";
import { PROFILE_INVALID, STATUS, TUNNEL_PROFILES, holdConnectionWrite, mockApi, mockTunnel } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";
import { setViewportWidth } from "../../test/viewport";
import { blankProfileForm, formToProfileIn, profileToForm } from "./profileForm";

afterEach(() => act(() => settleConfirm(false)));

async function openEditor(options: { phone?: boolean; profiles?: TuningProfile[] } = {}) {
  if (options.phone) setViewportWidth(390);
  const api$ = mockTunnel(mockApi());
  api$.getStatus.mockResolvedValue(STATUS);
  if (options.profiles) api$.listProfiles.mockResolvedValue(options.profiles);
  const view = renderApp("/tunnel/anti-dpi");
  await screen.findByRole(options.phone ? "list" : "table", { name: "Profiles" });
  return { api$, ...view };
}

const editor = () => screen.getByRole("region", { name: "Profile editor" });
const section = (name: string) => within(editor()).getByRole("group", { name: new RegExp(`^${name}`) });
const row = (id: number) => document.querySelector<HTMLElement>(`tr[data-profile-id="${id}"]`)!;

async function answer(text: string, button: string) {
  const ask = await screen.findByRole("dialog", { name: "Confirm" });
  expect(ask).toHaveTextContent(text);
  await userEvent.click(within(ask).getByRole("button", { name: button }));
}

describe("Anti-DPI › editor fields (T3)", () => {
  it("a new profile: sections in contract order, the backend's defaults, feature fields hidden until switched on", async () => {
    await openEditor();
    const legends = [...editor().querySelectorAll("fieldset > legend")].map((legend) => legend.textContent);
    expect(legends).toEqual([
      "Profile", "TLS fragmentation", "UDP noisedecoy packets vs DPI / active probing", "Muxxhttp nodes only — ignored on Vision",
      "XHTTP transportxhttp nodes only", "TLStls-mode nodes", "DNS & QUIC", "QUIC",
    ]);
    expect(within(editor()).getByLabelText("Name")).toHaveValue("");
    expect(within(editor()).getByLabelText("Fingerprint")).toHaveValue("chrome");
    expect(within(within(editor()).getByLabelText("Fingerprint")).getAllByRole("option").at(-1)).toHaveTextContent("(no mimicry — not recommended)");
    expect(within(editor()).getByRole("switch", { name: "TLS fragmentation" })).toHaveAttribute("aria-checked", "false");
    expect(within(editor()).queryByLabelText("Length")).toBeNull();
    expect(within(editor()).queryByLabelText("Concurrency")).toBeNull();
    expect(within(editor()).getByRole("switch", { name: "DoH" })).toHaveAttribute("aria-checked", "true");
    expect(within(editor()).getByLabelText("DoH URL")).toHaveAttribute("placeholder", "(default)");
    expect(within(editor()).getByRole("radio", { name: "allow" })).toBeChecked();
    expect(within(editor()).getByRole("radio", { name: "drop (block)" })).not.toBeChecked();
    expect(within(editor()).getByLabelText("Padding")).toHaveAttribute("placeholder", "100-1000");
    expect(within(editor()).getByRole("button", { name: "Create" })).toBeInTheDocument();

    await userEvent.click(within(editor()).getByRole("switch", { name: "TLS fragmentation" }));
    expect(within(editor()).getByLabelText("Packets")).toHaveValue("tlshello");
    expect(within(editor()).getByLabelText("Length")).toHaveValue("100-200");
    expect(within(editor()).getByLabelText("Interval, ms")).toHaveValue("10-20");
    await userEvent.click(within(editor()).getByRole("switch", { name: "Mux" }));
    expect(within(editor()).getByLabelText("Concurrency")).toHaveAttribute("placeholder", "(default)");
    expect(within(within(editor()).getByLabelText("xudpProxyUDP443")).getAllByRole("option").map((option) => option.textContent)).toEqual(["(default)", "reject", "allow", "skip"]);
    await userEvent.click(within(editor()).getByRole("switch", { name: "DoH" }));
    expect(within(editor()).queryByLabelText("DoH URL")).toBeNull();
    expect(screen.getByText("● unsaved changes")).toBeInTheDocument();
  });

  it("Edit loads every field; noise rows are labelled by number and keep their inputs when one above is removed", async () => {
    await openEditor();
    await userEvent.click(within(row(2)).getByRole("button", { name: "Edit fragment-tls" }));
    expect(within(editor()).getByLabelText("Name")).toHaveValue("fragment-tls");
    expect(within(editor()).getByLabelText("Length")).toHaveValue("100-200");
    expect(within(editor()).getByRole("radio", { name: "drop (block)" })).toBeChecked();
    const noise = section("UDP noise");
    expect(within(noise).getByLabelText("Noise 2 type")).toHaveValue("hex");
    expect(within(noise).getByLabelText("Noise 2 packet")).toHaveValue("0a0b0c0d");
    expect(noise).toHaveTextContent("2 / 32");
    const second = within(noise).getByLabelText("Noise 2 delay");
    await userEvent.click(within(noise).getByRole("button", { name: "Remove noise 1" }));
    expect(within(noise).getByLabelText("Noise 1 delay")).toBe(second);
    await userEvent.click(within(noise).getByRole("button", { name: "Add noise" }));
    expect(within(noise).getByLabelText("Noise 2 type")).toHaveValue("rand");
    expect(within(noise).getByLabelText("Noise 2 packet")).toHaveValue("50-150");
    expect(within(noise).getByLabelText("Noise 2 delay")).toHaveValue("10-16");

    // New starts over without asking, unsaved edits or not.
    await userEvent.click(within(editor()).getByRole("button", { name: "New" }));
    expect(screen.queryByRole("dialog", { name: "Confirm" })).toBeNull();
    expect(within(editor()).getByLabelText("Name")).toHaveValue("");
    expect(within(editor()).getByRole("heading", { name: "New profile" })).toBeInTheDocument();
  });

  it("no more than 32 noise rows", async () => {
    const full = { ...TUNNEL_PROFILES[1]!, noises: Array.from({ length: 32 }, () => ({ type: "rand", packet: "50-150", delay: "10-16" })) };
    await openEditor({ profiles: [TUNNEL_PROFILES[0]!, full] });
    await userEvent.click(within(row(2)).getByRole("button", { name: "Edit fragment-tls" }));
    expect(within(section("UDP noise")).getByRole("button", { name: "Add noise" })).toBeDisabled();
    expect(section("UDP noise")).toHaveTextContent("32 / 32");
  });

  it("shows each limit under its field and sends nothing until the form passes", async () => {
    const { api$ } = await openEditor();
    await userEvent.type(within(editor()).getByLabelText("Name"), "strict");
    await userEvent.click(within(editor()).getByRole("switch", { name: "TLS fragmentation" }));
    await userEvent.clear(within(editor()).getByLabelText("Length"));
    await userEvent.type(within(editor()).getByLabelText("Length"), "0");
    expect(within(editor()).getByLabelText("Length")).toHaveAccessibleDescription("length: N or A-B within 1..65535");
    fireEvent.change(within(editor()).getByLabelText("DoH URL"), { target: { value: "http://1.1.1.1/dns-query" } });
    expect(await within(editor()).findByText("DoH URL must be https:// with a host, or blank")).toBeInTheDocument();
    await userEvent.click(within(editor()).getByRole("button", { name: "Create" }));
    expect(api$.addProfile).not.toHaveBeenCalled();
    await userEvent.click(within(editor()).getByRole("button", { name: "Validate" }));
    expect(await within(editor()).findByText("✗ length: N or A-B within 1..65535")).toBeInTheDocument();
    expect(api$.validateProfile).not.toHaveBeenCalled();
  });
});

describe("Anti-DPI › Validate, Create / Save, New (T5)", () => {
  it("Validate checks what Save would send; an edit marks the result stale", async () => {
    const { api$ } = await openEditor();
    await userEvent.click(within(row(3)).getByRole("button", { name: "Edit mux-heavy" }));
    await userEvent.click(within(editor()).getByRole("button", { name: "Validate" }));
    expect(await within(editor()).findByText("✓ profile valid")).toHaveClass("text-ok");
    expect(api$.validateProfile).toHaveBeenCalledWith(formToProfileIn(profileToForm(TUNNEL_PROFILES[2]!)));
    expect(api$.validateProfile.mock.calls[0]![0]).toMatchObject({ name: "mux-heavy", mux_concurrency: "8", tls_max: "1.3", doh_url: "https://1.1.1.1/dns-query" });
    await userEvent.type(within(editor()).getByLabelText("ALPN"), ",h3");
    expect(within(editor()).getByRole("status")).toHaveTextContent("Form changed since this run — run it again.");
    api$.validateProfile.mockResolvedValueOnce(PROFILE_INVALID);
    await userEvent.click(within(editor()).getByRole("button", { name: "Validate" }));
    expect(await within(editor()).findByText("✗ bad fragment length '0-5'")).toHaveClass("text-bad");
  });

  it("Save of the live profile is a connection write; it says it was applied and resets the editor", async () => {
    const { api$, client } = await openEditor();
    const success = vi.spyOn(toast, "success");
    let finish: () => void = () => {};
    api$.updateProfile.mockImplementation((id, patch) => new Promise((resolve) => { finish = () => resolve({ ...TUNNEL_PROFILES[1]!, ...patch, id }); }));
    await userEvent.click(within(row(2)).getByRole("button", { name: "Edit fragment-tls" }));
    await userEvent.clear(within(editor()).getByLabelText("Length"));
    await userEvent.type(within(editor()).getByLabelText("Length"), "80-160");
    await userEvent.click(within(editor()).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.updateProfile).toHaveBeenCalledWith(2, { ...formToProfileIn(profileToForm(TUNNEL_PROFILES[1]!)), frag_length: "80-160" }));
    expect(client.isMutating({ mutationKey: CONNECTION_WRITE })).toBe(1);
    await act(async () => finish());
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved & applied to the live tunnel", { duration: 8000 }));
    await waitFor(() => expect(within(editor()).getByRole("heading", { name: "New profile" })).toBeInTheDocument());
    expect(within(editor()).getByLabelText("Name")).toHaveValue("");
    expect(screen.queryByText("● unsaved changes")).toBeNull();
  });

  it("Save of the live profile waits while another connection change runs; another profile saves meanwhile", async () => {
    const { client } = await openEditor();
    const release = holdConnectionWrite(client);
    await userEvent.click(within(row(2)).getByRole("button", { name: "Edit fragment-tls" }));
    await waitFor(() => expect(within(editor()).getByRole("button", { name: "Save" })).toBeDisabled());
    await userEvent.click(within(row(3)).getByRole("button", { name: "Edit mux-heavy" }));
    expect(within(editor()).getByRole("button", { name: "Save" })).toBeEnabled();
    await release();
  });

  it("a 502 on saving the live profile says nothing was saved, keeps the edit, and Save can be pressed again with the same body", async () => {
    const { api$ } = await openEditor();
    const error = vi.spyOn(toast, "error");
    const success = vi.spyOn(toast, "success");
    api$.updateProfile.mockRejectedValueOnce(new ApiError(502, "xray -test failed: bad config"));
    await userEvent.click(within(row(2)).getByRole("button", { name: "Edit fragment-tls" }));
    await userEvent.clear(within(editor()).getByLabelText("Length"));
    await userEvent.type(within(editor()).getByLabelText("Length"), "80-160");
    const expectedBody = { ...formToProfileIn(profileToForm(TUNNEL_PROFILES[1]!)), frag_length: "80-160" };
    await userEvent.click(within(editor()).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.updateProfile).toHaveBeenCalledWith(2, expectedBody));
    await waitFor(() => expect(error).toHaveBeenCalledWith("not saved — applying to the tunnel failed: xray -test failed: bad config", { duration: 20000 }));
    expect(success).not.toHaveBeenCalled();
    expect(within(editor()).getByLabelText("Length")).toHaveValue("80-160");
    expect(within(editor()).getByRole("heading", { name: "Editing profile · id 2" })).toBeInTheDocument();
    expect(screen.getByText("● unsaved changes")).toBeInTheDocument();

    await userEvent.click(within(editor()).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.updateProfile).toHaveBeenCalledTimes(2));
    expect(api$.updateProfile).toHaveBeenLastCalledWith(2, expectedBody);
  });

  it("a preset stages into a new profile under its name; Create sends it and starts over", async () => {
    const { api$ } = await openEditor();
    const success = vi.spyOn(toast, "success");
    await userEvent.click(within(editor()).getByRole("button", { name: "Stage preset…" }));
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "ru-hardenedRU-hardened — fragment + noise + QUIC drop", "stealth-latencyStealth (min latency) — fingerprint only, QUIC allowed", "cdn-xhttpCDN / XHTTP — padding + xmux",
    ]);
    await userEvent.click(items[0]!);
    await waitFor(() => expect(success).toHaveBeenCalledWith('preset "ru-hardened" staged into the editor — Create/Save to apply', { duration: 8000 }));
    expect(within(editor()).getByLabelText("Name")).toHaveValue("ru-hardened");
    expect(within(editor()).getByRole("switch", { name: "TLS fragmentation" })).toHaveAttribute("aria-checked", "true");
    expect(within(section("UDP noise")).getByLabelText("Noise 1 packet")).toHaveValue("50-150");
    expect(within(editor()).getByRole("radio", { name: "drop (block)" })).toBeChecked();
    expect(api$.listProfiles).toHaveBeenCalledTimes(1);

    await userEvent.click(within(editor()).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api$.addProfile).toHaveBeenCalledWith({
      ...blankProfileForm(), name: "ru-hardened", frag_enabled: true, quic: "drop", noise_enabled: true, noises: [{ type: "rand", packet: "50-150", delay: "10-16" }],
    }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("saved", { duration: 8000 }));
    await waitFor(() => expect(within(editor()).getByLabelText("Name")).toHaveValue(""));
  });

  it("a preset over unsaved edits asks first, and keeps the edited name", async () => {
    await openEditor();
    await userEvent.click(within(row(3)).getByRole("button", { name: "Edit mux-heavy" }));
    await userEvent.type(within(editor()).getByLabelText("Name"), "-2");
    await userEvent.click(within(editor()).getByRole("button", { name: "Stage preset…" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: /stealth-latency/ }));
    await answer("Discard unsaved profile changes?", "Discard");
    await waitFor(() => expect(within(editor()).getByLabelText("Fingerprint")).toHaveValue("chrome"));
    expect(within(editor()).getByLabelText("Name")).toHaveValue("mux-heavy-2");
    expect(within(editor()).getByRole("heading", { name: "Editing profile · id 3" })).toBeInTheDocument();
    expect(screen.getByText("● unsaved changes")).toBeInTheDocument();
  });

  it("Clone prefills a new profile; Edit and Clone over unsaved edits ask first; leaving asks too", async () => {
    const { api$, router } = await openEditor();
    await userEvent.click(within(row(3)).getByRole("button", { name: "Clone mux-heavy" }));
    expect(within(editor()).getByLabelText("Name")).toHaveValue("mux-heavy copy");
    expect(within(editor()).getByLabelText("Concurrency")).toHaveValue("8");
    await userEvent.type(within(editor()).getByLabelText("Name"), "!");
    await userEvent.click(within(row(1)).getByRole("button", { name: "Edit balanced" }));
    await answer("Discard unsaved profile changes?", "Cancel");
    expect(within(editor()).getByLabelText("Name")).toHaveValue("mux-heavy copy!");
    await userEvent.click(within(row(2)).getByRole("button", { name: "Clone fragment-tls" }));
    await answer("Discard unsaved profile changes?", "Cancel");
    act(() => void router.navigate({ to: "/tunnel/health" }));
    await answer("Discard unsaved changes and leave this screen?", "Cancel");
    expect(router.state.location.pathname).toBe("/tunnel/anti-dpi");
    await userEvent.click(within(editor()).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(api$.addProfile).toHaveBeenCalledWith(expect.objectContaining({ name: "mux-heavy copy!", mux_concurrency: "8", quic: "proxy" })));
  });
});

describe("Anti-DPI › Save blocked by a hidden or collapsed field (fix round 1)", () => {
  it("desktop: an invalid Mux concurrency left over when Mux is switched off blocks Save with a visible reason", async () => {
    const { api$ } = await openEditor();
    await userEvent.click(within(row(3)).getByRole("button", { name: "Edit mux-heavy" }));
    await userEvent.clear(within(editor()).getByLabelText("Concurrency"));
    await userEvent.type(within(editor()).getByLabelText("Concurrency"), "0");
    await userEvent.click(within(editor()).getByRole("switch", { name: "Mux" }));
    expect(within(editor()).queryByLabelText("Concurrency")).toBeNull();
    await userEvent.click(within(editor()).getByRole("button", { name: "Save" }));
    expect(await within(editor()).findByText("✗ concurrency: a number within 1..1024")).toHaveClass("text-bad");
    expect(api$.updateProfile).not.toHaveBeenCalled();
  });

  it("desktop: an invalid noise packet left over when UDP noise is switched off blocks Save with a visible reason", async () => {
    const { api$ } = await openEditor();
    await userEvent.click(within(row(2)).getByRole("button", { name: "Edit fragment-tls" }));
    await userEvent.clear(within(section("UDP noise")).getByLabelText("Noise 1 packet"));
    await userEvent.type(within(section("UDP noise")).getByLabelText("Noise 1 packet"), "0");
    await userEvent.click(within(editor()).getByRole("switch", { name: "UDP noise" }));
    expect(within(editor()).queryByLabelText("Noise 1 packet")).toBeNull();
    await userEvent.click(within(editor()).getByRole("button", { name: "Save" }));
    expect(await within(editor()).findByText("✗ packet: N or A-B within 1..65535")).toHaveClass("text-bad");
    expect(api$.updateProfile).not.toHaveBeenCalled();
  });

  it("phone: an invalid value in a collapsed section blocks Save with a visible reason and opens that section", async () => {
    const { api$ } = await openEditor({ phone: true });
    await userEvent.click(screen.getByRole("button", { name: "More actions for fragment-tls" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
    const page = await screen.findByRole("region", { name: "Profile editor" });
    const xhttp = within(page).getByRole("button", { name: /^XHTTP transport/ });
    await userEvent.click(xhttp);
    await userEvent.type(within(page).getByLabelText("Padding"), "abc");
    await userEvent.click(xhttp);
    expect(xhttp).toHaveAttribute("aria-expanded", "false");
    expect(xhttp).toHaveTextContent("custom");
    await userEvent.click(within(page).getByRole("button", { name: "Save" }));
    expect(await within(page).findByText("✗ padding: N or A-B within 0..1,000,000")).toHaveClass("text-bad");
    expect(xhttp).toHaveAttribute("aria-expanded", "true");
    expect(api$.updateProfile).not.toHaveBeenCalled();
  });
});

describe("Anti-DPI › phone editor (T3)", () => {
  it("collapsible sections open where a feature is on, with a short state when closed; a sticky Validate / Save", async () => {
    const { api$ } = await openEditor({ phone: true });
    await userEvent.click(screen.getByRole("button", { name: "More actions for fragment-tls" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
    const page = await screen.findByRole("region", { name: "Profile editor" });
    const frag = within(page).getByRole("button", { name: "TLS fragmentation" });
    expect(frag).toHaveAttribute("aria-expanded", "true");
    const mux = within(page).getByRole("button", { name: /^Muxxhttp nodes only — ignored on Vision/ });
    expect(mux).toHaveAttribute("aria-expanded", "false");
    expect(mux).toHaveTextContent("off");
    expect(within(page).queryByRole("switch", { name: "Mux" })).toBeNull();
    await userEvent.click(mux);
    expect(within(page).getByRole("switch", { name: "Mux" })).toBeVisible();
    expect(within(page).getByRole("button", { name: /^XHTTP transport/ })).toHaveTextContent("defaults");
    await userEvent.click(frag);
    expect(frag).toHaveAttribute("aria-expanded", "false");
    expect(frag).toHaveTextContent("on");
    expect(within(page).getByRole("button", { name: "Stage preset" })).toBeInTheDocument();

    await userEvent.type(within(page).getByLabelText("Name"), "-v2");
    await userEvent.click(within(page).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api$.updateProfile).toHaveBeenCalledWith(2, expect.objectContaining({ name: "fragment-tls-v2" })));
    expect(await screen.findByRole("list", { name: "Profiles" })).toBeInTheDocument();
  });

  it("Back with unsaved edits asks first", async () => {
    await openEditor({ phone: true });
    await userEvent.click(screen.getByRole("button", { name: "New profile" }));
    const page = await screen.findByRole("region", { name: "Profile editor" });
    await userEvent.type(within(page).getByLabelText("Name"), "draft");
    await userEvent.click(within(page).getByRole("button", { name: "Back to profiles" }));
    await answer("Discard unsaved profile changes?", "Discard");
    expect(await screen.findByRole("list", { name: "Profiles" })).toBeInTheDocument();
  });
});
