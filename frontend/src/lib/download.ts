/**
 * Save `text` as a file named `filename`, from memory: an object URL behind an `<a download>` that is clicked and then
 * removed, with the URL revoked on a later tick (revoking at once can cancel the download in some browsers). Nothing
 * is cached and nothing stays in the page.
 */
export function downloadText(filename: string, text: string, type = "text/plain"): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  window.setTimeout(() => {
    URL.revokeObjectURL(url);
    anchor.remove();
  }, 0);
}
