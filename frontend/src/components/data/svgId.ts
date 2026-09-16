import { useId } from "react";

/** A document-unique id usable inside url(#…): React's ids may contain characters a CSS url() would need escaped. */
export function useSvgId(prefix: string): string {
  return `${prefix}-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
}
