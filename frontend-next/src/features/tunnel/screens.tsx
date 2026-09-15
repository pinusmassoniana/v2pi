import { TabPlaceholder } from "../../app/pages/Placeholder";

// Tunnel's screens, loaded as one chunk when the section is first opened.
export { AntiDpi } from "./AntiDpiScreen";
export { Routing } from "./RoutingScreen";
export function Health() { return <TabPlaceholder path="/tunnel/health" />; }
