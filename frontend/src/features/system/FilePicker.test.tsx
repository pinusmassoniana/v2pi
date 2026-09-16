import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FilePicker } from "./FilePicker";

function file(name: string, text = "{}"): File {
  return new File([text], name, { type: "application/json" });
}

describe("FilePicker", () => {
  it("is a real file input named by its label, and reads what was picked", async () => {
    const onPick = vi.fn();
    render(<FilePicker label="Choose file…" file={null} onPick={onPick} />);
    const input = screen.getByLabelText("Choose file…");

    expect(input).toHaveAttribute("type", "file");
    expect(input).toHaveAttribute("accept", "application/json,.json");
    await userEvent.upload(input, file("v2pi-backup-2026-09-14.json"));

    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick.mock.calls[0]![0]).toMatchObject({ name: "v2pi-backup-2026-09-14.json" });
  });

  it("shows the filename and its size once one is picked, and says Change… instead", () => {
    render(<FilePicker label="Choose file…" file={file("v2pi-settings.json", "x".repeat(1_400))} onPick={vi.fn()} />);

    expect(screen.getByText("v2pi-settings.json")).toBeInTheDocument();
    expect(screen.getByText("1 KB")).toBeInTheDocument();
    expect(screen.getByLabelText("Change…")).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose file…")).toBeNull();
  });

  it("picking the same file twice still reports it — the operator may have edited it meanwhile", async () => {
    const onPick = vi.fn();
    render(<FilePicker label="Choose file…" file={null} onPick={onPick} />);
    const input = screen.getByLabelText("Choose file…") as HTMLInputElement;

    await userEvent.upload(input, file("same.json"));
    expect(input.value).toBe("");            // cleared, so the next change event fires
    await userEvent.upload(input, file("same.json"));

    expect(onPick).toHaveBeenCalledTimes(2);
  });

  it("locks while a write runs", () => {
    render(<FilePicker label="Choose file…" file={null} onPick={vi.fn()} disabled />);
    expect(screen.getByLabelText("Choose file…")).toBeDisabled();
  });
});
