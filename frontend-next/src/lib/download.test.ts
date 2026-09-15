import { afterEach, describe, expect, it, vi } from "vitest";
import { downloadText } from "./download";

afterEach(() => vi.useRealTimers());

describe("downloadText", () => {
  it("clicks a download anchor over an object URL of the text, then removes both on a later tick", async () => {
    vi.useFakeTimers();
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:e2e-phone"; });
    URL.revokeObjectURL = vi.fn();
    const clicked: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function click(this: HTMLAnchorElement) {
      expect(this.isConnected).toBe(true);
      clicked.push(this);
    });

    downloadText("e2e-phone.conf", "[General]\nbypass-system = true\n");

    expect(clicked).toHaveLength(1);
    expect(clicked[0]!.download).toBe("e2e-phone.conf");
    expect(clicked[0]!.getAttribute("href")).toBe("blob:e2e-phone");
    expect(blobs[0]!.type).toBe("text/plain");
    expect(await blobs[0]!.text()).toBe("[General]\nbypass-system = true\n");
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(0);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:e2e-phone");
    expect(clicked[0]!.isConnected).toBe(false);
  });

  it("takes another type", async () => {
    vi.useFakeTimers();
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:backup"; });
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    downloadText("backup.json", "{}", "application/json");
    expect(blobs[0]!.type).toBe("application/json");
  });
});
