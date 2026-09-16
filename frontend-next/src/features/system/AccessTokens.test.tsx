import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { TOKEN_WRITE } from "../../api/invalidation";
import { keys } from "../../api/keys";
import { settleConfirm } from "../../components/confirm";
import { AUDIT, NOW_SEC, TOKENS, TOKEN_CREATED, holdWrite, mockApi, mockSystem } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => act(() => settleConfirm(false)));

async function openAccess() {
  const api$ = mockSystem(mockApi());
  const view = renderApp("/system/access");
  await screen.findByRole("region", { name: "API tokens" });
  await screen.findByText("ci-deploy");
  return { api$, ...view };
}

const region = (name: string) => screen.getByRole("region", { name });

async function answer(label: string) {
  const dialog = await screen.findByRole("dialog", { name: "Confirm" });
  await userEvent.click(within(dialog).getByRole("button", { name: label }));
}

async function openForm() {
  await userEvent.click(within(region("API tokens")).getByRole("button", { name: "Create token" }));
  return screen.findByRole("region", { name: "New API token" });
}

describe("Access — tokens (G7)", () => {
  it("is a real table whose rows read the gateway's own fields", async () => {
    await openAccess();
    const table = within(region("API tokens")).getByRole("table", { name: "API tokens" });
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Name", "Scope", "Prefix", "Created", "Last used", "Expires", "Actions",
    ]);
    expect(within(region("API tokens")).getByText("3 issued")).toBeInTheDocument();

    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(TOKENS.length);
    // "—" and "never" are literal, and a near expiry earns a badge
    expect(within(rows[0]!).getByText("—")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("in 30 d")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("never")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("read/write")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("pgwp_Vt9pLs1")).toBeInTheDocument();
    // each row's button is named with its token, not just "Revoke"
    expect(within(rows[1]!).getByRole("button", { name: "Revoke ci-deploy" })).toBeInTheDocument();
    expect(region("API tokens")).toHaveTextContent("Last used is stamped at most once a minute");
    expect(region("API tokens")).toHaveTextContent("the prefix is not a secret");
  });

  it("says what each scope can actually do", async () => {
    await openAccess();
    const scopes = region("Scopes");
    expect(scopes).toHaveTextContent("Status and traffic history only.");
    expect(scopes).toHaveTextContent("/api/status, /api/traffic/history, /api/node-health, /api/network — every other path answers 403.");
    expect(scopes).toHaveTextContent("Every GET, including the backup.");
    expect(scopes).toHaveTextContent("Not a “safe” scope.");
    expect(scopes).toHaveTextContent("Every GET plus every mutation, with no CSRF header.");
  });

  it("creates with an expiry sent as an epoch, and sends none for “never”", async () => {
    const { api$ } = await openAccess();
    await openForm();
    await userEvent.type(screen.getByLabelText("Name"), "home-assistant");
    await userEvent.click(screen.getByRole("radio", { name: "30 days" }));
    expect(screen.getByText(/^sent as a timestamp · \d{4}-\d{2}-\d{2}$/)).toBeInTheDocument();

    await userEvent.click(within(region("New API token")).getByRole("button", { name: "Create token" }));

    await screen.findByText("token “home-assistant” created");
    expect(api$.createToken).toHaveBeenCalledWith("home-assistant", "monitor", NOW_SEC + 30 * 86_400);

    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await openForm();
    await userEvent.type(screen.getByLabelText("Name"), "forever");
    await userEvent.click(screen.getByRole("radio", { name: "read/write" }));
    await userEvent.click(within(region("New API token")).getByRole("button", { name: "Create token" }));

    await screen.findByText("token “forever” created");
    // "never" must not send `expires_at: null`: the schema refuses it.
    expect(api$.createToken).toHaveBeenLastCalledWith("forever", "readwrite", undefined);
  });

  it("shows the secret once: never in either cache, navigation blocked, a second create refused, gone on Done", async () => {
    const { api$, client, unmount } = await openAccess();
    // Both caches, asserted while a settled mutation is actually there so neither can pass vacuously:
    // TanStack keeps a settled mutation's state.data in the MutationCache until its gcTime expires
    // (5 minutes by default), which outlives both Done and this screen.
    const cached = () => {
      const mutations = client.getMutationCache().getAll();
      expect(mutations.length).toBeGreaterThan(0);
      return JSON.stringify([
        client.getQueryCache().getAll().map((query) => query.state.data),
        mutations.map((mutation) => mutation.state.data),
      ]);
    };
    await openForm();
    await userEvent.type(screen.getByLabelText("Name"), "home-assistant");
    await userEvent.click(within(region("New API token")).getByRole("button", { name: "Create token" }));

    const panel = await screen.findByRole("group", { name: "New token home-assistant" });
    expect(within(panel).getByText(TOKEN_CREATED.token)).toBeInTheDocument();
    expect(within(panel).getByText("Copy it now, it is shown only once. The gateway keeps only its hash — if you lose it, revoke this token and issue another.")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Leaving this screen loses the secret." })).toBeInTheDocument();
    // not in either cache, not in a toast, not in an attribute
    expect(cached()).not.toContain(TOKEN_CREATED.token);
    expect(document.body.innerHTML.split(TOKEN_CREATED.token).length - 1).toBe(1);
    expect(panel.querySelector(`[title*="${TOKEN_CREATED.token}"], [data-token]`)).toBeNull();

    // the guard is held whether or not anything was typed
    await userEvent.click(screen.getByRole("link", { name: "Overview" }));
    expect(await screen.findByRole("dialog", { name: "Confirm" })).toHaveTextContent("Discard unsaved changes and leave this screen?");
    await answer("Cancel");

    await userEvent.click(within(region("API tokens")).getByRole("button", { name: "Create token" }));
    await screen.findByText("Finish copying the visible token first.");
    expect(api$.createToken).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByText(TOKEN_CREATED.token)).toBeNull());
    expect(cached()).not.toContain(TOKEN_CREATED.token);

    // and it is still unreachable once the screen itself is gone
    unmount();
    expect(cached()).not.toContain(TOKEN_CREATED.token);
    expect(document.body.innerHTML).not.toContain(TOKEN_CREATED.token);
  });

  it("Copy puts the secret on the clipboard without rendering it anywhere else", async () => {
    const written: string[] = [];
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: async (text: string) => { written.push(text); } } });
    await openAccess();
    await openForm();
    await userEvent.type(screen.getByLabelText("Name"), "home-assistant");
    await userEvent.click(within(region("New API token")).getByRole("button", { name: "Create token" }));
    await screen.findByRole("group", { name: "New token home-assistant" });

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    await screen.findByRole("button", { name: "Copied ✓" });
    expect(written).toEqual([TOKEN_CREATED.token]);
    vi.unstubAllGlobals();
  });

  it("a 422 keeps the form open and says what the gateway said", async () => {
    const { api$ } = await openAccess();
    api$.createToken.mockRejectedValueOnce(new ApiError(422, "expires_at must be in the future"));
    await openForm();
    await userEvent.type(screen.getByLabelText("Name"), "skewed");
    await userEvent.click(screen.getByRole("radio", { name: "30 days" }));

    await userEvent.click(within(region("New API token")).getByRole("button", { name: "Create token" }));

    await screen.findByText("expires_at must be in the future");
    expect(screen.getByLabelText("Name")).toHaveValue("skewed");
  });

  it("asks before revoking, then says it is gone", async () => {
    const { api$ } = await openAccess();

    await userEvent.click(screen.getByRole("button", { name: "Revoke ci-deploy" }));
    expect(await screen.findByRole("dialog", { name: "Confirm" }))
      .toHaveTextContent("Revoke token “ci-deploy”? Anything using it stops working immediately.");
    await answer("Cancel");
    expect(api$.deleteToken).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Revoke ci-deploy" }));
    await answer("Revoke");
    await screen.findByText("token “ci-deploy” revoked");
    expect(api$.deleteToken).toHaveBeenCalledWith(2);
  });

  it("a second revoke is not a failure: the row was already gone, and the list is re-read", async () => {
    const { api$, client } = await openAccess();
    api$.deleteToken.mockRejectedValueOnce(new ApiError(404, "token not found"));
    const invalidate = vi.spyOn(client, "invalidateQueries");

    await userEvent.click(screen.getByRole("button", { name: "Revoke ci-deploy" }));
    await answer("Revoke");

    await screen.findByText("that token is already gone");
    await waitFor(() => expect(invalidate.mock.calls.some(([filters]) => String(filters?.queryKey) === String(keys.tokens))).toBe(true));
  });

  it("sends nothing when another token write started while the question was open", async () => {
    const { api$, client } = await openAccess();
    await userEvent.click(screen.getByRole("button", { name: "Revoke ci-deploy" }));
    const release = holdWrite(client, TOKEN_WRITE);
    await answer("Revoke");

    await screen.findByText("Another token change is still running — try again when it finishes");
    expect(api$.deleteToken).not.toHaveBeenCalled();
    await release();
  });

  it("shows an empty state", async () => {
    const api$ = mockSystem(mockApi());
    api$.listTokens.mockResolvedValue([]);
    renderApp("/system/access");
    expect(await screen.findByText("No tokens yet.")).toBeInTheDocument();
  });

  it("a list that failed to load is an error with Retry, and Retry re-reads it", async () => {
    const api$ = mockSystem(mockApi());
    api$.listTokens.mockRejectedValue(new ApiError(500, "tokens unreadable"));
    renderApp("/system/access");

    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert).toHaveTextContent("API tokens did not load");

    api$.listTokens.mockResolvedValue(TOKENS);
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));

    await screen.findByText("ci-deploy");
    expect(api$.listTokens.mock.calls.length).toBeGreaterThan(1);
  });
});

describe("Access — audit log (G8)", () => {
  it("fetches nothing until Show, then reads once and keeps reading nothing", async () => {
    const { api$ } = await openAccess();
    const audit = region("Audit log");
    expect(api$.listAudit).not.toHaveBeenCalled();
    expect(within(audit).getByText("not loaded")).toBeInTheDocument();
    expect(audit).toHaveTextContent("Nothing is fetched until Show is pressed");

    await userEvent.click(within(audit).getByRole("button", { name: "Show" }));

    await within(audit).findByRole("list", { name: "Audit entries" });
    expect(api$.listAudit).toHaveBeenCalledTimes(1);
    expect(within(audit).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(within(audit).getByText("last 100")).toBeInTheDocument();
    expect(audit).toHaveTextContent("Newest first, one shot of at most 100 rows — nothing polls this and nothing refreshes it after a write.");
  });

  it("renders every row's method, path and outcome, and colours the outcome by its class", async () => {
    await openAccess();
    const audit = region("Audit log");
    await userEvent.click(within(audit).getByRole("button", { name: "Show" }));
    const rows = within(await within(audit).findByRole("list", { name: "Audit entries" })).getAllByRole("listitem");

    expect(rows).toHaveLength(AUDIT.length);
    expect(within(rows[1]!).getByText("/api/settings")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("token:pgwp_Vt9pLs1")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("→ 200")).toHaveClass("text-t2");
    expect(within(rows[4]!).getByText("→ 403")).toHaveClass("text-warn");
    expect(within(rows[7]!).getByText("→ 413")).toHaveClass("text-warn");
    // the middleware's own 413 is recorded before the session is read — the card says so
    expect(audit).toHaveTextContent("the 413 was refused by the body limit before the session was even checked");
  });

  it("a remote-access client uuid is absent from the page until Reveal, and re-masks on unmount", async () => {
    const full = AUDIT[0]!.path;
    const { unmount } = await openAccess();
    const audit = region("Audit log");
    await userEvent.click(within(audit).getByRole("button", { name: "Show" }));
    await within(audit).findByRole("list", { name: "Audit entries" });

    expect(screen.getByText("/api/rw/clients/3f2a1c4e…")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(full);

    const reveal = screen.getByRole("button", { name: "Reveal uuid in /api/rw/clients/3f2a1c4e…" });
    expect(reveal).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(reveal);

    expect(await screen.findByText(full)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reveal uuid in /api/rw/clients/3f2a1c4e…" })).toHaveAttribute("aria-pressed", "true");
    unmount();
    expect(document.body.innerHTML).not.toContain(full);
  });

  it("says which of its three states it is in", async () => {
    const api$ = mockSystem(mockApi());
    api$.listAudit.mockRejectedValue(new ApiError(500, "could not read the audit log"));
    renderApp("/system/access");
    const audit = await screen.findByRole("region", { name: "Audit log" });

    await userEvent.click(within(audit).getByRole("button", { name: "Show" }));
    expect(await within(audit).findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent("could not read the audit log");

    api$.listAudit.mockResolvedValue([]);
    await userEvent.click(within(audit).getAllByRole("button", { name: "Retry" })[0]!);
    expect(await within(audit).findByText("No recorded changes yet.")).toBeInTheDocument();
  });
});
