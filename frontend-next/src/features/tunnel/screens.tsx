import { TabPlaceholder } from "../../app/pages/Placeholder";

// Tunnel's screens, loaded as one chunk when the section is first opened.
export { Routing } from "./RoutingScreen";
export function AntiDpi() { return <TabPlaceholder path="/tunnel/anti-dpi" />; }
export function Health() { return <TabPlaceholder path="/tunnel/health" />; }
