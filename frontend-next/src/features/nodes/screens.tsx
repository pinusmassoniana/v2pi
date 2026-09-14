import { NodeDetailPlaceholder, TabPlaceholder } from "../../app/pages/Placeholder";

// Nodes' screens, loaded as one chunk when the section is first opened.
export function Servers() { return <TabPlaceholder path="/nodes" />; }
export function Subscriptions() { return <TabPlaceholder path="/nodes/subscriptions" />; }
export const NodeDetail = NodeDetailPlaceholder;
