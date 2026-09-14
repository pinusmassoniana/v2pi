import { useParams } from "@tanstack/react-router";
import { EmptyState } from "../../components/ui/States";
import { sectionForPath, titleForPath, type AppPath } from "../nav";

// Each screen is replaced by its section's milestone; until then this marks where it will live.
export function Placeholder({ section, title }: { section: string; title: string }) {
  return (
    <EmptyState title={`${section} › ${title}`}>
      This screen is being rebuilt. Until it lands here, the current panel keeps serving it.
    </EmptyState>
  );
}

export function TabPlaceholder({ path }: { path: AppPath }) {
  return <Placeholder section={sectionForPath(path).label} title={titleForPath(path)} />;
}

export function NodeDetailPlaceholder() {
  const { nodeId } = useParams({ from: "/nodes/$nodeId" });
  return <Placeholder section="Nodes" title={`Node ${nodeId}`} />;
}
