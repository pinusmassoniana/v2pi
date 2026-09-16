import { useCallback, useMemo, useReducer } from "react";
import type { Routing } from "../../api/client";
import { changeCount, isStaged, stagedFromRouting, type StagedRouting } from "./rules";

/** An edit in progress: the gateway ruleset it started from, and the ruleset as edited. */
export interface EditSession {
  base: StagedRouting;
  current: StagedRouting;
}

export type EditorAction =
  /** Change the edited ruleset — opening a session from the gateway's ruleset (`server`) when none is staged. */
  | { type: "edit"; server: StagedRouting; change: (state: StagedRouting) => StagedRouting }
  /** Replace the staged ruleset (a preset, an import, Reset): a new session from the gateway's ruleset as it is now. */
  | { type: "replace"; server: StagedRouting; next: StagedRouting }
  /** Drop the session: the editor shows the gateway's ruleset again. */
  | { type: "discard" };

/**
 * A session that no longer differs from where it started is as good as none: the next change starts over from the
 * gateway's ruleset as it is then, so a poll that landed meanwhile is not overwritten. A replacement always starts
 * over — the staged rules it replaces were discarded first.
 */
export function editorReducer(session: EditSession | null, action: EditorAction): EditSession | null {
  if (action.type === "discard") return null;
  if (action.type === "replace") return { base: action.server, current: action.next };
  const open = session && isStaged(session.base, session.current) ? session : { base: action.server, current: action.server };
  return { base: open.base, current: action.change(open.current) };
}

export interface RoutingEditor {
  /** The ruleset on screen: the staged one while staged, the gateway's otherwise; null until it has loaded. */
  current: StagedRouting | null;
  /** Whether the ruleset on screen differs from the one editing started from. */
  staged: boolean;
  /** The staged banner's "N changes". */
  changes: number;
  /** Staged, and the gateway's ruleset is no longer the one editing started from. */
  gatewayChanged: boolean;
  edit: (change: (state: StagedRouting) => StagedRouting) => void;
  replace: (next: StagedRouting) => void;
  discard: () => void;
}

/**
 * R5 state: the editor is seeded from the polled ruleset and follows it while nothing is staged; once something is,
 * it keeps the edits and only reports that the gateway's ruleset moved on. The callbacks change only when the
 * gateway's ruleset does (the query keeps unchanged data referentially equal), so memoised rows keep them.
 */
export function useRoutingEditor(routing: Routing | undefined): RoutingEditor {
  const server = useMemo(() => (routing ? stagedFromRouting(routing) : null), [routing]);
  const [session, dispatch] = useReducer(editorReducer, null);
  const staged = session !== null && isStaged(session.base, session.current);
  const edit = useCallback((change: (state: StagedRouting) => StagedRouting) => {
    if (server) dispatch({ type: "edit", server, change });
  }, [server]);
  const replace = useCallback((next: StagedRouting) => {
    if (server) dispatch({ type: "replace", server, next });
  }, [server]);
  const discard = useCallback(() => dispatch({ type: "discard" }), []);
  return {
    current: staged ? session.current : server,
    staged,
    changes: staged ? changeCount(session.base, session.current) : 0,
    gatewayChanged: staged && server !== null && isStaged(session.base, server),
    edit,
    replace,
    discard,
  };
}
