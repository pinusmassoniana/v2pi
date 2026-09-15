import { useCallback, useMemo, useState } from "react";
import type { Node } from "../../api/client";
import { ExportDialog } from "./ExportDialog";
import { ImportSheet } from "./ImportSheet";
import { NodeFormSheet } from "./NodeFormSheet";
import type { NodeMenuCallbacks } from "./NodeRowActions";

export type NodeDialogState =
  | { kind: "add" }
  | { kind: "clone"; node: Node }
  | { kind: "edit"; node: Node }
  | { kind: "import" }
  | { kind: "export"; node: Node }
  | null;

/** Which node form or dialog a screen has open, and stable callbacks to open each. */
export function useNodeDialogs() {
  const [dialog, setDialog] = useState<NodeDialogState>(null);
  const menu = useMemo<NodeMenuCallbacks>(() => ({
    onEdit: (node) => setDialog({ kind: "edit", node }),
    onClone: (node) => setDialog({ kind: "clone", node }),
    onExport: (node) => setDialog({ kind: "export", node }),
  }), []);
  const openAdd = useCallback(() => setDialog({ kind: "add" }), []);
  const openImport = useCallback(() => setDialog({ kind: "import" }), []);
  const close = useCallback(() => setDialog(null), []);
  return { dialog, menu, openAdd, openImport, close };
}

/** The open form or dialog; each mounts when opened, so it takes its values then and forgets them on close. */
export function NodeDialogs({ dialog, onClose }: { dialog: NodeDialogState; onClose: () => void }) {
  if (dialog === null) return null;
  switch (dialog.kind) {
    case "add":
      return <NodeFormSheet mode="add" onClose={onClose} />;
    case "clone":
      return <NodeFormSheet key={`clone-${dialog.node.id}`} mode="clone" node={dialog.node} onClose={onClose} />;
    case "edit":
      return <NodeFormSheet key={`edit-${dialog.node.id}`} mode="edit" node={dialog.node} onClose={onClose} />;
    case "import":
      return <ImportSheet onClose={onClose} />;
    case "export":
      return <ExportDialog node={dialog.node} onClose={onClose} />;
  }
}
