import { useMutation } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { useId, useState } from "react";
import { errText } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { useUnsavedGuard } from "../../app/guard";
import { closeGuarded } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { notifyOk } from "../../components/ui/Toaster";
import { importedMessage } from "./subForm";

/** N17: the backend reads at most 512 KiB. */
export const IMPORT_MAX_BYTES = 512 * 1024;

const PLACEHOLDER = `vless://uuid@host:443?type=tcp&security=reality…
— or —
proxies:
  - name: …
    type: vless
— or —
[ { "name": …, "address": … } ]`;

/** N17: paste vless / base64 lines, a Clash proxies: list or a JSON node list; they become manual servers. */
export function ImportSheet({ onClose }: { onClose: () => void }) {
  const id = useId();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const importWrite = useApiWrite("importNodes");
  const dirty = text.trim() !== "";
  useUnsavedGuard(dirty);
  const tooBig = new TextEncoder().encode(text).length > IMPORT_MAX_BYTES;
  // The result is said even if the sheet has gone; closing it or showing the failure in it is passed to mutate,
  // which runs only while this sheet is still open for its latest import.
  const run = useMutation({
    mutationFn: (input: string) => importWrite(input),
    onSuccess: (result) => notifyOk(importedMessage(result)),
  });
  function start() {
    run.mutate(text, {
      onSuccess: () => onClose(),
      // 422 "parse failed: …" and 409: nothing was imported; the text stays for another try.
      onError: (failure) => setError(errText(failure, "import failed")),
    });
  }

  return (
    <Sheet open dirty={dirty} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent title="Import servers" className="max-md:h-[96dvh] max-md:max-h-[96dvh]">
        <div className="flex flex-col gap-3">
          <div className="flex items-baseline justify-between gap-2">
            <label htmlFor={id} className="text-xs font-semibold text-t2">Nodes to import</label>
            <span className="text-[11px] text-t3">≤ 512 KiB</span>
          </div>
          <textarea
            id={id}
            value={text}
            // Locked while the import runs: its answer is about the text that was sent, so that text stays on screen.
            readOnly={run.isPending}
            onChange={(event) => {
              setText(event.target.value);
              setError(null);
            }}
            rows={10}
            placeholder={PLACEHOLDER}
            spellCheck={false}
            className="w-full resize-y rounded-xl border border-line bg-glass-2 p-3 font-mono text-xs text-t1 placeholder:text-t3 focus-visible:outline-2 focus-visible:outline-g2"
          />
          <p className="text-xs text-t2">
            base64 / vless lines, a Clash <code className="font-mono">proxies:</code> YAML, or a JSON node list — duplicates of manual servers are skipped.
            Up to 500 nodes, all or nothing: one bad entry rejects the whole import.
          </p>
          {tooBig ? <p role="alert" className="text-xs text-bad">The list is larger than 512 KiB.</p> : null}
          {error ? <p role="alert" className="whitespace-pre-wrap text-xs text-bad">{error}</p> : null}
          <div className="flex justify-end gap-2">
            <Button onClick={() => void closeGuarded(dirty, onClose)}>Cancel</Button>
            <Button variant="primary" disabled={!dirty || tooBig || run.isPending} onClick={start}>
              <Upload size={14} aria-hidden />
              {run.isPending ? "Importing…" : "Import"}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
