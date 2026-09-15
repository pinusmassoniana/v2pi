import { NodeDetailPlaceholder, TabPlaceholder } from "../../app/pages/Placeholder";

// Nodes' screens, loaded as one chunk when the section is first opened.
export { Servers } from "./ServersScreen";
export function Subscriptions() { return <TabPlaceholder path="/nodes/subscriptions" />; }
export const NodeDetail = NodeDetailPlaceholder;
