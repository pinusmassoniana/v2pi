/**
 * Copy through a selection and `execCommand("copy")`: the one way left where the page has no Clipboard API (plain
 * HTTP) — which is how the panel is served, so on a phone this is the only path there is.
 *
 * The field is left editable (never `readOnly`, `contenteditable` set) and is focused and selected by range rather
 * than by `select()`: iOS Safari copies nothing from a `readOnly` textarea, and nothing from a selection it was not
 * given explicitly. It sits off-screen, is removed again, and focus goes back where the click left it.
 */
function copyBySelection(text: string): boolean {
  if (typeof document.execCommand !== "function") return false;
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("contenteditable", "true");
  field.setAttribute("aria-hidden", "true");
  field.style.position = "fixed";
  field.style.top = "0";
  field.style.left = "-9999px";
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.body.appendChild(field);
  try {
    field.focus();
    field.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    field.remove();
    focused?.focus();
  }
}

/**
 * Put text that is still being fetched on the clipboard. Where the page has `ClipboardItem`, the write starts right
 * away, inside the click — Safari keeps the user's activation only for a write begun before any await — with the text
 * as its promise. Otherwise, once the text arrives: `clipboard.writeText`, then a selection copy, which also works
 * over plain HTTP, where `navigator.clipboard` does not exist. The text never stays in the page.
 *
 * Rejects with the text's own error when it could not be read (so the caller reports why), and with "copy failed"
 * when nothing could write it.
 */
export async function copyDeferred(text: Promise<string>): Promise<void> {
  const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
  let written: Promise<boolean> | null = null;
  if (clipboard && typeof clipboard.write === "function" && typeof ClipboardItem === "function") {
    const blob = text.then((value) => new Blob([value], { type: "text/plain" }));
    blob.catch(() => {});   // a text that fails is reported below, from `await text`
    written = clipboard.write([new ClipboardItem({ "text/plain": blob })]).then(() => true, () => false);
  }
  const value = await text;
  if (written && (await written)) return;
  if (clipboard && typeof clipboard.writeText === "function") {
    try {
      await clipboard.writeText(value);
      return;
    } catch {}
  }
  if (!copyBySelection(value)) throw new Error("copy failed");
}

/** Put `text` on the clipboard (the ways and failures of copyDeferred). */
export function copyText(text: string): Promise<void> {
  return copyDeferred(Promise.resolve(text));
}
