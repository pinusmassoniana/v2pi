import { useQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { useId, useState } from "react";
import type { Node } from "../../api/client";
import { queries } from "../../api/keys";
import { Button } from "../../components/ui/Button";
import { Dialog, DialogContent } from "../../components/ui/Dialog";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { flagEmoji } from "../../lib/flag";
import { nodeJson, vlessUri } from "./vless";

/** Copy one text; the button reads "Copied" for a moment and says so to screen readers. */
function CopyBlock({ label, name, text, rows }: { label: string; name: string; text: string; rows: number }) {
  const id = useId();
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      notifyOk("copied");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      notifyError(null, "copy failed");
    }
  }
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-xs font-semibold text-t2">{label}</label>
        <Button size="sm" aria-label={`Copy ${name}`} onClick={() => void copy()}>
          <Copy size={14} aria-hidden />
          <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
        </Button>
      </div>
      <textarea
        id={id}
        readOnly
        value={text}
        rows={rows}
        spellCheck={false}
        className="w-full resize-y rounded-xl border border-line bg-glass-2 p-3 font-mono text-[11.5px] text-t1"
      />
    </div>
  );
}

/** N16: the node as a vless:// link and as JSON, each with its own Copy. */
export function ExportDialog({ node, onClose }: { node: Node; onClose: () => void }) {
  const health = useQuery(queries.nodeHealth()).data?.find((row) => row.node_id === node.id);
  const flag = flagEmoji(health?.egress_cc);
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent title={`Export ${flag ? `${flag} ` : ""}${node.name}`}>
        <div className="flex flex-col gap-3">
          <CopyBlock label="vless:// link" name="link" text={vlessUri(node)} rows={4} />
          <CopyBlock label="JSON · full node" name="JSON" text={nodeJson(node)} rows={10} />
          <p className="text-[11px] text-t3">flow only for vision · pbk / sid only for reality · path / host / mode only for xhttp · IPv6 hosts are bracketed</p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
