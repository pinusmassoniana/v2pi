import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { STATUS } from "../test/fixtures";
import { AuthGate, resolvePhase, useAuth } from "./auth";

function Protected() {
  const { logout } = useAuth();
  return <button onClick={() => void logout()}>Log out</button>;
}

function renderGate(client = new QueryClient()) {
  render(<QueryClientProvider client={client}><AuthGate><Protected /></AuthGate></QueryClientProvider>);
  return client;
}

afterEach(() => vi.unstubAllGlobals());

describe("resolvePhase", () => {
  it("server unreachable → offline", async () => {
    vi.spyOn(api, "getSetup").mockRejectedValue(new ApiError(0, "network error"));
    await expect(resolvePhase()).resolves.toEqual({ kind: "offline" });
  });

  it("first run → setup; the bootstrap proof is required unless the server says otherwise", async () => {
    const getSetup = vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: true });
    await expect(resolvePhase()).resolves.toEqual({ kind: "setup", bootstrapRequired: true });
    getSetup.mockResolvedValue({ needs_setup: true, bootstrap_required: false });
    await expect(resolvePhase()).resolves.toEqual({ kind: "setup", bootstrapRequired: false });
  });

  it("provisioned with a live session → authed; without one → login", async () => {
    vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: false });
    const getStatus = vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    await expect(resolvePhase()).resolves.toEqual({ kind: "authed" });
    getStatus.mockRejectedValue(new ApiError(401, "unauthorized"));
    await expect(resolvePhase()).resolves.toEqual({ kind: "login" });
  });
});

describe("AuthGate", () => {
  it("an unreachable server at boot offers Retry and recovers", async () => {
    const getSetup = vi.spyOn(api, "getSetup")
      .mockRejectedValueOnce(new ApiError(0, "network error"))
      .mockResolvedValue({ needs_setup: false });
    vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    renderGate();
    expect(await screen.findByText("Can't reach the panel server.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(getSetup).toHaveBeenCalledTimes(2);
  });

  it("first-run setup validates, sends the trimmed proof, and opens the app", async () => {
    vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: true, bootstrap_required: true });
    const setup = vi.spyOn(api, "setup").mockResolvedValue({ ok: true });
    vi.spyOn(api, "ensureCsrf").mockResolvedValue("csrf");
    renderGate();
    await userEvent.type(await screen.findByLabelText("One-time bootstrap token"), "  proof  ");
    await userEvent.type(screen.getByPlaceholderText("username"), "admin");
    await userEvent.type(screen.getByPlaceholderText("password"), "long-enough");
    await userEvent.type(screen.getByPlaceholderText("confirm password"), "different!!");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("passwords don't match");
    expect(setup).not.toHaveBeenCalled();

    await userEvent.clear(screen.getByPlaceholderText("confirm password"));
    await userEvent.type(screen.getByPlaceholderText("confirm password"), "long-enough");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(setup).toHaveBeenCalledWith("admin", "long-enough", "proof");
  });

  it("login shows the backend's error, then opens the app", async () => {
    vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: false });
    vi.spyOn(api, "getStatus").mockRejectedValue(new ApiError(401, "unauthorized"));
    const login = vi.spyOn(api, "login")
      .mockRejectedValueOnce(new ApiError(401, "bad password"))
      .mockResolvedValue({ ok: true });
    vi.spyOn(api, "ensureCsrf").mockResolvedValue("csrf");
    renderGate();
    await userEvent.type(await screen.findByPlaceholderText("username"), "admin");
    await userEvent.type(screen.getByPlaceholderText("password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("bad password");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("button", { name: "Log out" })).toBeInTheDocument();
    expect(login).toHaveBeenCalledTimes(2);
  });

  it("a 401 mid-session returns to Login with nothing of the old session cached", async () => {
    vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: false });
    vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    const client = renderGate();
    await screen.findByRole("button", { name: "Log out" });
    client.setQueryData(["nodes"], [{ id: 1 }]);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    await expect(api.listNodes()).rejects.toMatchObject({ status: 401 });
    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument();
    expect(client.getQueryData(["nodes"])).toBeUndefined();
  });

  it("log out ends the session and clears the cache", async () => {
    vi.spyOn(api, "getSetup").mockResolvedValue({ needs_setup: false });
    vi.spyOn(api, "getStatus").mockResolvedValue(STATUS);
    const logout = vi.spyOn(api, "logout").mockResolvedValue({ ok: true });
    const client = renderGate();
    await screen.findByRole("button", { name: "Log out" });
    client.setQueryData(["nodes"], [{ id: 1 }]);
    await userEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument();
    expect(logout).toHaveBeenCalled();
    expect(client.getQueryData(["nodes"])).toBeUndefined();
  });
});
