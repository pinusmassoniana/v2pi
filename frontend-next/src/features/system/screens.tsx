import { TabPlaceholder } from "../../app/pages/Placeholder";

// System's screens, loaded as one chunk when the section is first opened.
export { Backups } from "./BackupsScreen";
export { Access } from "./AccessScreen";
export function Logs() { return <TabPlaceholder path="/system/logs" />; }
export function Panel() { return <TabPlaceholder path="/system/panel" />; }
