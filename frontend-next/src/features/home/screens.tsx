import { TabPlaceholder } from "../../app/pages/Placeholder";

// Home's screens, loaded as one chunk when the section is first opened.
export function Overview() { return <TabPlaceholder path="/" />; }
export function Traffic() { return <TabPlaceholder path="/traffic" />; }
