import { Activity, Ellipsis } from "lucide-react";
import type { Node } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "../../components/ui/DropdownMenu";
import { cn } from "../../lib/cn";
import { useNodeConnection, useNodeRemoval, useNodeTest } from "./useNodeActions";

/** Edit, Clone and Export open a form or dialog the screen owns; each item shows only when its handler is given. */
export interface NodeMenuCallbacks {
  onEdit?: (node: Node) => void;
  onClone?: (node: Node) => void;
  onExport?: (node: Node) => void;
}

export const NO_MENU_CALLBACKS: NodeMenuCallbacks = {};

export const DISCONNECT_FIRST = "Disconnect first";

/** Let the menu close and hand focus back before an item opens a dialog of its own. */
function afterMenu(action: () => void): void {
  window.setTimeout(action, 0);
}

export interface NodeRowActionsProps {
  node: Node;
  active: boolean;
  menu?: NodeMenuCallbacks;
  /** Phone cards show Connect and Test only; editing lives on the node page. */
  withMenu?: boolean;
  className?: string;
}

/** N6, N7 and the ⋯ menu (N13–N16): Connect or Disconnect, Test, then Edit, Clone, Export and Delete or Detach. */
export function NodeRowActions({ node, active, menu = NO_MENU_CALLBACKS, withMenu = true, className }: NodeRowActionsProps) {
  const connection = useNodeConnection(node, active);
  const test = useNodeTest(node);
  const removal = useNodeRemoval();
  const manual = node.subscription_id === null;
  const { onEdit, onClone, onExport } = menu;

  return (
    <div className={cn("flex items-center justify-end gap-1.5", className)}>
      <Button
        size="sm"
        variant={active ? "danger" : "primary"}
        disabled={connection.disabled}
        title={connection.reason ?? undefined}
        aria-label={`${connection.label} ${node.name}`}
        onClick={connection.toggle}
      >
        {connection.label}
      </Button>
      <Button
        size="icon"
        className="size-8"
        disabled={test.busy}
        aria-label={test.busy ? `Testing ${node.name}` : `Test ${node.name}`}
        title="Test: TCP, HTTP and a real request through this node"
        onClick={test.run}
      >
        <Activity size={15} aria-hidden className={test.busy ? "animate-pulse" : undefined} />
      </Button>
      {withMenu ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon" variant="ghost" className="size-8" aria-label={`More actions for ${node.name}`}>
              <Ellipsis size={16} aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            {onEdit ? (
              <DropdownMenuItem disabled={active} hint={active ? DISCONNECT_FIRST : undefined} onSelect={() => afterMenu(() => onEdit(node))}>
                Edit
              </DropdownMenuItem>
            ) : null}
            {onClone ? <DropdownMenuItem onSelect={() => afterMenu(() => onClone(node))}>Clone</DropdownMenuItem> : null}
            {onExport ? <DropdownMenuItem onSelect={() => afterMenu(() => onExport(node))}>Export</DropdownMenuItem> : null}
            {onEdit || onClone || onExport ? <DropdownMenuSeparator /> : null}
            {manual ? (
              <DropdownMenuItem
                disabled={active || removal.busy}
                hint={active ? DISCONNECT_FIRST : undefined}
                onSelect={() => afterMenu(() => void removal.deleteNode(node))}
                className="text-bad"
              >
                Delete…
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem disabled={removal.busy} onSelect={() => afterMenu(() => void removal.detachNode(node))}>
                Detach to Servers
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </div>
  );
}
