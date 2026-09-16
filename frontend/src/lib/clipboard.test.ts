import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { copyDeferred, copyText } from "./clipboard";

const LINK = "vless://uuid@home.example.org:443?security=reality#iphone";

function setClipboard(value: Partial<Clipboard> | undefined) {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value });
}

function setClipboardItem(value: unknown) {
  Object.defineProperty(globalThis, "ClipboardItem", { configurable: true, writable: true, value });
}

function setExecCommand(value: unknown) {
  Object.defineProperty(document, "execCommand", { configurable: true, writable: true, value });
}

class FakeClipboardItem {
  constructor(public items: Record<string, Promise<Blob>>) {}
}

afterEach(() => {
  setClipboard(undefined);
  setClipboardItem(undefined);
  setExecCommand(undefined);
});

describe("copyDeferred", () => {
  it("starts a ClipboardItem write inside the click, before the text has arrived", async () => {
    let resolve: (value: string) => void = () => {};
    const text = new Promise<string>((done) => { resolve = done; });
    const write = vi.fn<(items: FakeClipboardItem[]) => Promise<void>>(async () => {});
    const writeText = vi.fn();
    setClipboard({ write, writeText } as unknown as Clipboard);
    setClipboardItem(FakeClipboardItem);

    const copied = copyDeferred(text);
    expect(write).toHaveBeenCalledTimes(1);   // synchronously, in the same task as the click
    resolve(LINK);
    await copied;
    const blob = await write.mock.calls[0]![0][0]!.items["text/plain"]!;
    expect(await blob.text()).toBe(LINK);
    expect(writeText).not.toHaveBeenCalled();
  });

  it("without ClipboardItem it waits for the text and uses writeText", async () => {
    const writeText = vi.fn(async () => {});
    setClipboard({ writeText } as unknown as Clipboard);
    await copyDeferred(Promise.resolve(LINK));
    expect(writeText).toHaveBeenCalledWith(LINK);
  });

  it("over plain HTTP, with no Clipboard API at all, it copies through a selection and leaves nothing in the page", async () => {
    const selected: string[] = [];
    const execCommand = vi.fn((command: string) => {
      const field = document.querySelector("textarea");
      selected.push(
        `${command}:${field?.value}:${field?.readOnly}:${field?.getAttribute("contenteditable")}`
        + `:${field?.selectionStart}-${field?.selectionEnd}:focused=${document.activeElement === field}`,
      );
      return true;
    });
    setExecCommand(execCommand);
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    await copyText(LINK);
    // The field iOS Safari actually copies from: focused, not readOnly, contenteditable, and selected by range.
    expect(selected).toEqual([`copy:${LINK}:false:true:0-${LINK.length}:focused=true`]);
    expect(document.querySelector("textarea")).toBeNull();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("falls back to a selection when the Clipboard API refuses", async () => {
    setClipboard({ write: vi.fn(async () => { throw new DOMException("denied", "NotAllowedError"); }), writeText: vi.fn(async () => { throw new Error("denied"); }) } as unknown as Clipboard);
    setClipboardItem(FakeClipboardItem);
    const execCommand = vi.fn(() => true);
    setExecCommand(execCommand);
    await copyText(LINK);
    expect(execCommand).toHaveBeenCalledWith("copy");
  });

  it("says copy failed when nothing could write it", async () => {
    setExecCommand(vi.fn(() => false));
    await expect(copyText(LINK)).rejects.toThrow("copy failed");
    setExecCommand(undefined);
    await expect(copyText(LINK)).rejects.toThrow("copy failed");
  });

  it("when the text itself could not be read, rejects with that error and writes nothing", async () => {
    const writeText = vi.fn(async () => {});
    const write = vi.fn(async () => {});
    setClipboard({ write, writeText } as unknown as Clipboard);
    setClipboardItem(FakeClipboardItem);
    const refused = new ApiError(422, "set the external endpoint (DDNS name or WAN IP) first");
    await expect(copyDeferred(Promise.reject(refused))).rejects.toBe(refused);
    expect(writeText).not.toHaveBeenCalled();
  });
});
