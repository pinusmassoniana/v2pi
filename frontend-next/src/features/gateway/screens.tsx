import { TabPlaceholder } from "../../app/pages/Placeholder";

// Gateway's screens, loaded as one chunk when the section is first opened.
export function Network() { return <TabPlaceholder path="/gateway/network" />; }
export function RemoteAccess() { return <TabPlaceholder path="/gateway/remote-access" />; }
