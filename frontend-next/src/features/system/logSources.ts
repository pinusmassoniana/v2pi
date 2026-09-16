// System › Logs: the four sources, what they are, and the client-side filter. Pure and unit-tested.

export interface LogSource {
  value: string;
  label: string;
  /**
   * Where the lines come from — a DISPLAY-ONLY constant, never a value from the gateway. `GET /logs` answers
   * `{source, lines}` only, and the absolute paths the gateway holds are a disclosure the screen has no reason to print.
   */
  origin: string;
  /** xray is built with `"loglevel": "warning"`, `"access": "none"` and no error path, so it writes neither file. */
  emptyByConfig: boolean;
}

export const LOG_SOURCES: readonly LogSource[] = [
  { value: "app", label: "app", origin: "data/app.log", emptyByConfig: false },
  { value: "xray-stderr", label: "xray output", origin: "in memory · redacted, last 8 KB", emptyByConfig: false },
  { value: "xray-error", label: "xray error file", origin: "data/xray-error.log", emptyByConfig: true },
  { value: "xray-access", label: "xray access file", origin: "data/xray-access.log", emptyByConfig: true },
];

/** The screen opens here, not on the backend's `xray-error` default, which is one of the two permanently empty files. */
export const DEFAULT_LOG_SOURCE = "app";
export const DEFAULT_LOG_LINES = 200;
export const MIN_LOG_LINES = 1;
export const MAX_LOG_LINES = 1000;

export function logSource(value: string): LogSource | undefined {
  return LOG_SOURCES.find((source) => source.value === value);
}

export function logSourceLabel(value: string): string {
  return logSource(value)?.label ?? value;
}

/** Case-insensitive substring over the lines already loaded. Never part of the query key: the gateway never sees it. */
export function filterLines(lines: readonly string[], query: string): string[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...lines];
  return lines.filter((line) => line.toLowerCase().includes(needle));
}

export interface LinePart {
  text: string;
  match: boolean;
}

/** One line split into the pieces the pane highlights: every occurrence of the filter term, case-insensitively. */
export function highlightParts(line: string, query: string): LinePart[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [{ text: line, match: false }];
  const parts: LinePart[] = [];
  const haystack = line.toLowerCase();
  let at = 0;
  for (let found = haystack.indexOf(needle, at); found !== -1; found = haystack.indexOf(needle, at)) {
    if (found > at) parts.push({ text: line.slice(at, found), match: false });
    parts.push({ text: line.slice(found, found + needle.length), match: true });
    at = found + needle.length;
  }
  if (at < line.length) parts.push({ text: line.slice(at), match: false });
  return parts;
}

export interface ParsedLogLine {
  time: string;
  level: string;
  logger: string;
  message: string;
}

const APP_LINE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (\w+) (\S+) ([\s\S]*)$/;

/**
 * An app-log line as its formatter wrote it (`%(asctime)s %(levelname)s %(name)s %(message)s`). xray's own output is
 * not formatted that way, so it comes back unparsed and is rendered as one plain message.
 */
export function parseLogLine(line: string): ParsedLogLine | null {
  const match = APP_LINE.exec(line);
  if (!match) return null;
  return { time: match[1]!, level: match[2]!, logger: match[3]!, message: match[4]! };
}

/** The last path segment of a logger name, for a phone where the whole dotted name does not fit. */
export function shortLogger(logger: string): string {
  const at = logger.lastIndexOf(".");
  return at === -1 ? logger : `…${logger.slice(at + 1)}`;
}

/** Time of day only — the pane already says which day it is loading. */
export function shortTime(time: string): string {
  return time.includes(" ") ? time.slice(time.indexOf(" ") + 1) : time;
}

export const EMPTY_BY_CONFIG =
  'This one is empty by configuration: the panel builds xray with "loglevel": "warning", "access": "none" and no ' +
  "error path, so xray writes neither file. Its output is in xray output instead.";

/**
 * What an empty pane says. `logs.tail` answers `[]` for a missing file, an unreadable one and an empty one alike, so
 * for a source that CAN have content the sentence must not claim the log is empty. For the two files xray is not
 * configured to write, the reason is known and is named instead. `xray-stderr` is neither a file nor a log that is
 * simply "empty": it is the supervisor's in-memory tail, reset to `""` on every `start()`, so its own empty reading
 * names the real cause instead of borrowing the generic file wording.
 */
export function emptyMessage(source: string): string {
  if (logSource(source)?.emptyByConfig) return EMPTY_BY_CONFIG;
  if (source === "xray-stderr") {
    return "Nothing to show — xray has not written anything yet, typically because it has not run since the panel started.";
  }
  return "Nothing to show — this log is empty, or the gateway has no file for it yet.";
}

/** Only `xray-stderr` is a character-capped tail, so only it can start mid-line. */
export function truncationNote(source: string): string | null {
  if (source !== "xray-stderr") return null;
  return "the panel keeps the last 8 KB of xray's output, so the first line here can be a fragment";
}

export const LOGS_EXPLAINER =
  "xray output is the panel's own redacted copy of what xray printed — the only place xray's errors live. xray error " +
  "file and xray access file are files xray is not configured to write, so they answer empty until that changes.";

export const LOGS_REDACTION_NOTE =
  "The app log can name interfaces, addresses and the commands the panel runs on the host. It is not redacted.";

/** Design §2's "Logs · auto-refresh copy" — pinned word for word, rendered under the toolbar next to Auto-refresh. */
export function autoRefreshNote(): string {
  return "While it is on, this screen is the only thing polling — every 5 s for the current source and line count. " +
    "Changing either starts a new read and drops the old one; leaving the screen switches it off.";
}

/** Before Load: the screen reads nothing on its own, and says what pressing it will do. */
export function beforeLoadMessage(lines: number): string {
  return `Press Load to read the last ${lines} lines.`;
}

/** The footer's first segment: how much of what was loaded is on screen, and under which filter. */
export function showingLabel(shown: number, total: number, query: string): string {
  const head = `showing ${shown} of ${total} lines`;
  return query.trim() ? `${head} · filter “${query.trim()}”` : head;
}

/** The footer's last segment. Download writes what the pane shows, not everything that was loaded. */
export function downloadNote(shown: number, total: number): string {
  return `Download writes the ${shown} lines you are looking at, not all ${total}`;
}
