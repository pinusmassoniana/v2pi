import { TabPlaceholder } from "../../app/pages/Placeholder";

// Gateway's screens, loaded as one chunk when the section is first opened.
export { Network } from "./NetworkScreen";
export function RemoteAccess() { return <TabPlaceholder path="/gateway/remote-access" />; }
