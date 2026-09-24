import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Settings } from "../../api/client";
import { PASSWORD_WRITE, SETTINGS_WRITE, TOKEN_WRITE } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { SETTINGS, TOKENS, holdWrite, mockApi, mockSystem } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => act(() => settleConfirm(false)));

async function openAccess(over: Partial<Settings> = {}) {
  const api$ = mockSystem(mockApi());
  let stored: Settings = { ...SETTINGS, ...over };
  api$.getSettings.mockImplementation(async () => stored);
  api$.putSettings.mockImplementation(async (patch: Partial<Settings>) => { stored = { ...stored, ...patch }; return stored; });
  const view = renderApp("/system/access");
  await screen.findByRole("region", { name: "Password" });
  return { api$, ...view };
}

const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement;
const changeButton = () => within(screen.getByRole("region", { name: "Password" })).getByRole("button", { name: /^(Change password|Changing…)$/ });

async function fillPasswords(current = "old-password", next = "New-passw0rd!", confirm = next) {
  await userEvent.type(field("Current password"), current);
  await userEvent.type(field("New password"), next);
  await userEvent.type(field("Confirm new password"), confirm);
}

async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

describe("Access — password (P4)", () => {
  it("is three real password fields with one reveal for all of them", async () => {
    await openAccess();
    expect(field("Current password")).toHaveAttribute("type", "password");
    expect(field("Current password")).toHaveAttribute("autocomplete", "current-password");
    expect(field("New password")).toHaveAttribute("autocomplete", "new-password");
    expect(field("Confirm new password")).toHaveAttribute("autocomplete", "new-password");

    const show = within(screen.getByRole("region", { name: "Password" })).getByRole("button", { name: "Show" });
    expect(show).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(show);

    for (const label of ["Current password", "New password", "Confirm new password"]) {
      expect(field(label)).toHaveAttribute("type", "text");
    }
    expect(screen.getByRole("button", { name: "Hide" })).toHaveAttribute("aria-pressed", "true");
  });

  it("scores the new password in words as well as bars, and says when the confirmation differs", async () => {
    await openAccess();
    expect(changeButton()).toBeDisabled();

    await userEvent.type(field("Current password"), "old-password");
    await userEvent.type(field("New password"), "short");
    expect(await screen.findByText("too short (min 8)")).toBeInTheDocument();
    await userEvent.clear(field("New password"));
    await userEvent.type(field("New password"), "aaaaaaaa");
    expect(await screen.findByText("weak")).toBeInTheDocument();
    await userEvent.clear(field("New password"));
    await userEvent.type(field("New password"), "New-passw0rd!");
    expect(await screen.findByText("strong")).toBeInTheDocument();

    await userEvent.type(field("Confirm new password"), "New-passw0rd");
    expect(await screen.findByText("does not match")).toBeInTheDocument();
    expect(changeButton()).toBeDisabled();
    // the result line is one live region, already mounted, that now says what is wrong
    const status = within(screen.getByRole("region", { name: "Password" })).getByRole("status");
    expect(status).toHaveTextContent("passwords do not match");

    await userEvent.type(field("Confirm new password"), "!");
    await waitFor(() => expect(changeButton()).toBeEnabled());
  });

  it("asks first, naming how many tokens it will delete, and Cancel sends nothing", async () => {
    const { api$ } = await openAccess();
    await waitFor(() => expect(api$.listTokens).toHaveBeenCalled());
    await fillPasswords();

    await userEvent.click(changeButton());
    const dialog = await screen.findByRole("dialog", { name: "Confirm" });
    expect(dialog).toHaveTextContent(
      `Change the password? Every other signed-in session is signed out, and all ${TOKENS.length} API tokens are deleted — anything using them stops working until you issue new ones.`,
    );
    await answer("Cancel");

    expect(api$.changePassword).not.toHaveBeenCalled();
    expect(field("Current password")).toHaveValue("old-password");
  });

  it("drops the clause at zero", async () => {
    const api$ = mockSystem(mockApi());
    api$.listTokens.mockResolvedValue([]);
    renderApp("/system/access");
    await screen.findByRole("region", { name: "Password" });
    await waitFor(() => expect(screen.getByText(/Changing it signs out every other session — this browser stays signed in/)).toBeInTheDocument());
    await fillPasswords();

    await userEvent.click(changeButton());

    expect(await screen.findByRole("dialog", { name: "Confirm" }))
      .toHaveTextContent("Change the password? Every other signed-in session is signed out.");
  });

  it("keeps the warning without a number when the token list failed — not known is not zero", async () => {
    const api$ = mockSystem(mockApi());
    api$.listTokens.mockRejectedValue(new ApiError(500, "tokens unreadable"));
    renderApp("/system/access");
    await screen.findByRole("region", { name: "Password" });
    await screen.findByText(/deletes every API token/, {}, { timeout: 3000 });
    await fillPasswords();

    await userEvent.click(changeButton());

    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent(
      "Change the password? Every other signed-in session is signed out, and every API token is deleted — anything using them stops working until you issue new ones.",
    );
  });

  it("changes it, clears the three fields, and re-reads the tokens the rotation deleted", async () => {
    const { api$, client } = await openAccess();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await waitFor(() => expect(api$.listTokens).toHaveBeenCalled());
    await fillPasswords();

    await userEvent.click(changeButton());
    await answer("Change password");

    await screen.findByText(`password changed · other sessions signed out · ${TOKENS.length} API tokens revoked`);
    expect(api$.changePassword).toHaveBeenCalledWith("old-password", "New-passw0rd!");
    for (const label of ["Current password", "New password", "Confirm new password"]) expect(field(label)).toHaveValue("");
    await waitFor(() => expect(invalidate.mock.calls.some(([filters]) => filters?.queryKey === keys.tokens)).toBe(true));
    // Neither password is a mutation variable: TanStack keeps a settled mutation's state.variables in the
    // MutationCache for its gcTime, so a leak here would outlive the form having cleared them.
    const mutations = client.getMutationCache().getAll();
    expect(mutations.length).toBeGreaterThan(0);
    const serialized = JSON.stringify(mutations.map((m) => m.state.variables));
    expect(serialized).not.toContain("old-password");
    expect(serialized).not.toContain("New-passw0rd!");
  });

  it("a wrong current password lands on that field, not in a banner, and neither password reaches the MutationCache", async () => {
    const { api$, client } = await openAccess();
    api$.changePassword.mockRejectedValueOnce(new ApiError(403, "current password incorrect"));
    await fillPasswords();

    await userEvent.click(changeButton());
    await answer("Change password");

    const error = await screen.findByText("current password incorrect");
    expect(field("Current password")).toHaveAttribute("aria-describedby", error.id);
    expect(field("Current password")).toHaveFocus();
    // nothing is cleared: the operator retypes one field, not three
    expect(field("New password")).toHaveValue("New-passw0rd!");
    // A failed attempt is a settled mutation too — its variables must not carry the passwords either.
    const serialized = JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables));
    expect(serialized).not.toContain("old-password");
    expect(serialized).not.toContain("New-passw0rd!");
  });

  it("a request that never answered keeps the fields and re-reads the tokens, because it may have committed", async () => {
    const { api$, client } = await openAccess();
    api$.changePassword.mockRejectedValueOnce(new ApiError(0, "request timed out"));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await fillPasswords();

    await userEvent.click(changeButton());
    await answer("Change password");

    const toast = await screen.findByText("no answer yet — the gateway may still be applying; reloading");
    expect(toast.closest("[data-sonner-toast]")).toHaveAttribute("data-type", "warning");
    expect(field("Current password")).toHaveValue("old-password");
    await waitFor(() => expect(invalidate.mock.calls.some(([filters]) => filters?.queryKey === keys.tokens)).toBe(true));
  });

  it("sends nothing when a token or settings write started while the question was open", async () => {
    const { api$, client } = await openAccess();
    await fillPasswords();
    await userEvent.click(changeButton());
    let release = holdWrite(client, TOKEN_WRITE);
    await answer("Change password");
    await screen.findByText("Another token change is still running — try again when it finishes");
    expect(api$.changePassword).not.toHaveBeenCalled();
    await release();

    await userEvent.click(changeButton());
    release = holdWrite(client, SETTINGS_WRITE);
    await answer("Change password");
    await screen.findByText("Another settings change is still running — try again when it finishes");
    expect(api$.changePassword).not.toHaveBeenCalled();
    await release();
  });

  it("Clear empties every field, and a password write locks the form", async () => {
    const { client } = await openAccess();
    await fillPasswords();
    await userEvent.click(within(screen.getByRole("region", { name: "Password" })).getByRole("button", { name: "Clear" }));
    for (const label of ["Current password", "New password", "Confirm new password"]) expect(field(label)).toHaveValue("");

    const release = holdWrite(client, PASSWORD_WRITE);
    await waitFor(() => expect(field("Current password")).toBeDisabled());
    await release();
    await waitFor(() => expect(field("Current password")).toBeEnabled());
  });
});

describe("Access — idle timeout (G5)", () => {
  it("saves on blur, not per keystroke, and says which way it went", async () => {
    const { api$ } = await openAccess({ session_timeout_min: 0 });
    const timeout = field("Idle timeout");
    await waitFor(() => expect(timeout).toHaveValue(0));

    await userEvent.clear(timeout);
    await userEvent.type(timeout, "30");
    expect(api$.putSettings).not.toHaveBeenCalled();     // "3" was never sent

    await userEvent.tab();
    await screen.findByText("idle timeout · 30 min");
    expect(api$.putSettings).toHaveBeenCalledTimes(1);
    expect(api$.putSettings).toHaveBeenCalledWith({ session_timeout_min: 30 });
  });

  it("Enter saves it too, and 0 says it is off", async () => {
    const { api$ } = await openAccess({ session_timeout_min: 30 });
    const timeout = field("Idle timeout");
    await waitFor(() => expect(timeout).toHaveValue(30));

    await userEvent.clear(timeout);
    await userEvent.type(timeout, "0{Enter}");

    await screen.findByText("idle timeout off");
    expect(api$.putSettings).toHaveBeenCalledWith({ session_timeout_min: 0 });
  });

  it("an unchanged value sends nothing, and a value that is not a whole number is refused in the gateway's words", async () => {
    const { api$ } = await openAccess({ session_timeout_min: 30 });
    const timeout = field("Idle timeout");
    await waitFor(() => expect(timeout).toHaveValue(30));

    await userEvent.click(timeout);
    await userEvent.tab();
    expect(api$.putSettings).not.toHaveBeenCalled();

    await userEvent.clear(timeout);
    await userEvent.type(timeout, "-5");
    await userEvent.tab();
    expect(await screen.findByText("session_timeout_min must be >= 0")).toBeInTheDocument();
    expect(api$.putSettings).not.toHaveBeenCalled();
  });

  it("puts the saved value back when the gateway refuses it", async () => {
    const { api$ } = await openAccess({ session_timeout_min: 15 });
    api$.putSettings.mockRejectedValueOnce(new ApiError(422, "session_timeout_min must be >= 0"));
    const timeout = field("Idle timeout");
    await waitFor(() => expect(timeout).toHaveValue(15));

    await userEvent.clear(timeout);
    await userEvent.type(timeout, "45");
    await userEvent.tab();

    await screen.findByText("session_timeout_min must be >= 0");
    await waitFor(() => expect(field("Idle timeout")).toHaveValue(15));
  });

  it("says it changes nothing for anyone signed in now", async () => {
    await openAccess();
    const card = screen.getByRole("region", { name: "Session" });
    expect(card).toHaveTextContent("An open, visible tab keeps polling, so this measures time with the panel closed or in the background.");
    expect(card).toHaveTextContent("Changing it ends nobody's session now");
    expect(within(card).getByText("saved as soon as you change it")).toBeInTheDocument();
  });

  it("settings that failed to load are said under the idle timeout, with Retry", async () => {
    const api$ = mockSystem(mockApi());
    api$.getSettings.mockRejectedValue(new ApiError(500, "boom"));
    renderApp("/system/access");
    await screen.findByRole("region", { name: "Password" });
    const session = screen.getByRole("region", { name: "Session" });
    expect(await within(session).findByText("Settings did not load — the saved idle timeout is unknown", {}, { timeout: 3000 })).toBeInTheDocument();
    api$.getSettings.mockResolvedValue(SETTINGS);
    await userEvent.click(within(session).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(field("Idle timeout")).toHaveValue(SETTINGS.session_timeout_min));
  });
});
