import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { keys } from "../../api/keys";
import { LOG_LINES, mockApi, mockSystem } from "../../test/fixtures";
import { renderApp } from "../../test/renderApp";

afterEach(() => vi.useRealTimers());

async function openLogs() {
  const api$ = mockSystem(mockApi());
  const view = renderApp("/system/logs");
  await screen.findByRole("region", { name: "Logs" });
  return { api$, ...view };
}

const pane = () => screen.getByRole("region", { name: "Log output" });
const load = () => screen.getByRole("button", { name: /^(Load|Loading…)$/ });

describe("Logs (G9)", () => {
  it("reads nothing on mount and says what Load will do", async () => {
    const { api$ } = await openLogs();

    expect(api$.getLogs).not.toHaveBeenCalled();
    expect(pane()).toHaveTextContent("Press Load to read the last 200 lines.");
    expect(screen.getByRole("radio", { name: "app" })).toBeChecked();
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("offers the four sources, with the panel's own copy of xray's output among them", async () => {
    await openLogs();
    const group = screen.getByRole("group", { name: "Log source" });
    expect(within(group).getAllByRole("radio").map((radio) => radio.getAttribute("value"))).toEqual([
      "app", "xray-stderr", "xray-error", "xray-access",
    ]);
    expect(within(group).getByRole("radio", { name: "xray output" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Logs" })).toHaveTextContent(
      "xray output is the panel's own redacted copy of what xray printed — the only place xray's errors live.",
    );
  });

  it("states the pinned auto-refresh explainer under the toolbar", async () => {
    await openLogs();

    expect(screen.getByRole("region", { name: "Logs" })).toHaveTextContent(
      "While it is on, this screen is the only thing polling — every 5 s for the current source and line count. " +
        "Changing either starts a new read and drops the old one; leaving the screen switches it off.",
    );
  });

  it("coerces a fractional line count to a whole number before it reaches the gateway", async () => {
    const { api$ } = await openLogs();
    const linesField = screen.getByLabelText("lines");
    await userEvent.clear(linesField);
    await userEvent.type(linesField, "10.5");

    await userEvent.click(load());

    await waitFor(() => expect(api$.getLogs).toHaveBeenCalledWith("app", 10));
  });

  it("the Load label follows the operator's own load, not the background poll", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    const { api$ } = await openLogs();
    await userEvent.click(load());
    await waitFor(() => expect(api$.getLogs).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("switch", { name: "Auto-refresh" }));

    let resolveNext: (value: { source: string; lines: string[] }) => void = () => {};
    api$.getLogs.mockImplementationOnce(() => new Promise((resolve) => { resolveNext = resolve; }));
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    await waitFor(() => expect(api$.getLogs).toHaveBeenCalledTimes(2));

    // the background poll is in flight, but the operator never pressed Load: the label must not flicker
    expect(screen.getByRole("button", { name: "Load" })).toBeInTheDocument();

    resolveNext({ source: "app", lines: LOG_LINES });
    await waitFor(() => expect(screen.getByRole("button", { name: "Load" })).toBeInTheDocument());
  });

  it("Load reads the default source once, at the default line count", async () => {
    const { api$ } = await openLogs();

    await userEvent.click(load());

    await waitFor(() => expect(api$.getLogs).toHaveBeenCalledWith("app", 200));
    expect(api$.getLogs).toHaveBeenCalledTimes(1);
    expect(await within(pane()).findByText(/stats client reconfigured/)).toBeInTheDocument();
    expect(screen.getByText(`showing ${LOG_LINES.length} of ${LOG_LINES.length} lines`)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Logs" })).toHaveTextContent("source app · data/app.log");
  });

  it("tints every level with a theme token, so the pane stays legible on both themes", async () => {
    await openLogs();
    await userEvent.click(load());
    await within(pane()).findByText(/stats client reconfigured/);

    const tone = (level: string) => within(pane()).getAllByText(level)[0]!.className;
    expect(tone("ERROR")).toContain("text-bad");
    expect(tone("WARNING")).toContain("text-warn");
    expect(tone("INFO")).toContain("text-t2");
    // A raw colour is theme-blind, and INFO is the majority of every app-log read: the cyan literal
    // this replaced measured 1.4:1 against the light theme's pane.
    expect(pane().innerHTML).not.toMatch(/text-\[#/);
  });

  it("changing the source or the line count makes a new key and leaves exactly one observer armed", async () => {
    const { api$, client } = await openLogs();
    await userEvent.click(load());
    await within(pane()).findByText(/stats client reconfigured/);

    await userEvent.click(screen.getByRole("radio", { name: "xray output" }));
    // the screen is back to "not loaded": a new key is never read behind the operator's back
    expect(pane()).toHaveTextContent("Press Load to read the last 200 lines.");
    await userEvent.click(load());
    await waitFor(() => expect(api$.getLogs).toHaveBeenLastCalledWith("xray-stderr", 200));

    const armed = client.getQueryCache().getAll().filter((query) => query.queryKey[0] === "logs" && query.observers.length > 0);
    expect(armed).toHaveLength(1);
    expect(armed[0]!.queryKey).toEqual(keys.logs("xray-stderr", 200));
  });

  it("the filter never reaches the query key: it filters at render and marks the matches", async () => {
    const { api$, client } = await openLogs();
    await userEvent.click(load());
    await within(pane()).findByText(/stats client reconfigured/);

    await userEvent.type(screen.getByPlaceholderText("filter…"), "SIGKILL");

    await waitFor(() => expect(screen.getByText(`showing 1 of ${LOG_LINES.length} lines · filter “SIGKILL”`)).toBeInTheDocument());
    expect(api$.getLogs).toHaveBeenCalledTimes(1);
    expect(client.getQueryCache().getAll().map((query) => query.queryKey).filter((key) => key[0] === "logs")).toEqual([keys.logs("app", 200)]);
    expect(within(pane()).getByText("SIGKILL").tagName).toBe("MARK");
    expect(screen.getByText(`Download writes the 1 lines you are looking at, not all ${LOG_LINES.length}`)).toBeInTheDocument();
  });

  it("auto-refresh is the only thing polling, and it is off until it is switched on", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    const { api$ } = await openLogs();
    await userEvent.click(load());
    await waitFor(() => expect(api$.getLogs).toHaveBeenCalledTimes(1));

    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(api$.getLogs).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("switch", { name: "Auto-refresh" }));
    expect(await screen.findByText("auto-refresh · every 5 s")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(api$.getLogs).toHaveBeenCalledTimes(1 + 60_000 / 5_000);
  });

  it("Download writes the filtered lines under the source's name", async () => {
    const blobs: Blob[] = [];
    const names: string[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:log"; });
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function mock(this: HTMLAnchorElement) { names.push(this.download); });
    await openLogs();
    await userEvent.click(load());
    await within(pane()).findByText(/stats client reconfigured/);
    await userEvent.type(screen.getByPlaceholderText("filter…"), "ERROR");

    await waitFor(() => expect(screen.getByRole("button", { name: "Download" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Download" }));

    await screen.findByText("app.log downloaded · 2 lines");
    expect(names[0]).toBe("app.log");
    const text = await blobs[0]!.text();
    expect(text.split("\n")).toHaveLength(2);
    expect(text).not.toContain("stats client reconfigured");
  });

  it("a source that is empty by configuration says why; one that could have content does not claim to be empty", async () => {
    await openLogs();

    await userEvent.click(screen.getByRole("radio", { name: "xray error file" }));
    await userEvent.click(load());
    expect(await within(pane()).findByText(/This one is empty by configuration/)).toHaveTextContent(
      'the panel builds xray with "loglevel": "warning", "access": "none" and no error path, so xray writes neither file. Its output is in xray output instead.',
    );

    await userEvent.click(screen.getByRole("radio", { name: "xray output" }));
    await userEvent.click(load());
    // Current copy (features/system/logSources.ts emptyMessage): xray-stderr names its own cause instead of the
    // generic file wording — see integration-notes.md "Task 4 fix round 1 → Task 10".
    expect(
      await within(pane()).findByText(
        "Nothing to show — xray has not written anything yet, typically because it has not run since the panel started.",
      ),
    ).toBeInTheDocument();
    // only the in-memory tail can start mid-line
    expect(screen.getByText("the panel keeps the last 8 KB of xray's output, so the first line here can be a fragment")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Logs" })).toHaveTextContent("source xray output · in memory · redacted, last 8 KB");
  });

  it("an error is a state above the pane, never a fake line inside it", async () => {
    const { api$ } = await openLogs();
    api$.getLogs.mockRejectedValue(new ApiError(400, "unknown log source"));

    await userEvent.click(load());

    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert).toHaveTextContent("Could not read the log — unknown log source");
    expect(pane()).not.toHaveTextContent("unknown log source");
    expect(pane().textContent).toBe("");
  });

  it("says the app log is not redacted, and is a keyboard-reachable pane", async () => {
    await openLogs();
    await userEvent.click(load());
    await within(pane()).findByText(/stats client reconfigured/);

    expect(screen.getByRole("region", { name: "Logs" })).toHaveTextContent(
      "The app log can name interfaces, addresses and the commands the panel runs on the host. It is not redacted.",
    );
    expect(pane().querySelector("pre")).toHaveAttribute("tabindex", "0");
    // the pane is not a live region: a 5 s refresh announcing all of it would be unusable
    expect(pane().querySelector("[aria-live]")).toBeNull();
    expect(pane()).not.toHaveAttribute("role", "status");
  });
});
