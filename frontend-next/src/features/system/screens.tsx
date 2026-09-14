import { TabPlaceholder } from "../../app/pages/Placeholder";

// System's screens, loaded as one chunk when the section is first opened.
export function Backups() { return <TabPlaceholder path="/system/backups" />; }
export function Access() { return <TabPlaceholder path="/system/access" />; }
export function Logs() { return <TabPlaceholder path="/system/logs" />; }
export function Panel() { return <TabPlaceholder path="/system/panel" />; }
