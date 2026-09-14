import { House, Route, Router, Server, Settings2, type LucideIcon } from "lucide-react";
import type { SectionId } from "../nav";

export const SECTION_ICONS: Record<SectionId, LucideIcon> = {
  home: House,
  nodes: Server,
  tunnel: Route,
  gateway: Router,
  system: Settings2,
};
