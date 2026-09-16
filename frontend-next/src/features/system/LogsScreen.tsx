import { useState } from "react";
import { LOGS_POLL_MS } from "../../api/cadence";
import { queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { CardHeader } from "../../components/data/CardHeader";
import { Chip } from "../../components/data/Chip";
import { Button } from "../../components/ui/Button";
import { SegmentedField } from "../../components/ui/Field";
import { GlassCard } from "../../components/ui/GlassCard";
import { Input } from "../../components/ui/Input";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { ErrorState } from "../../components/ui/States";
import { Toggle } from "../../components/ui/Toggle";
import { notifyOk } from "../../components/ui/Toaster";
import { downloadText } from "../../lib/download";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { cn } from "../../lib/cn";
import {
  DEFAULT_LOG_LINES, DEFAULT_LOG_SOURCE, LOGS_EXPLAINER, LOGS_REDACTION_NOTE, LOG_SOURCES, MAX_LOG_LINES, MIN_LOG_LINES,
  autoRefreshNote, beforeLoadMessage, downloadNote, emptyMessage, filterLines, highlightParts, logSource, logSourceLabel,
  parseLogLine, shortLogger, shortTime, showingLabel, truncationNote,
} from "./logSources";

// Tokens only, and each one measured against the pane on BOTH themes (`bg-glass` over the card's own
// glass): bad 5.4 / 5.6, warn 5.1 / 8.4, t2 8.4 / 7.3, t3 5.7 / 4.3 (light / dark). INFO is the body
// token rather than an accent — it is the majority of every app-log read, and colour is left to the two
// levels that mean something. The mockup's cyan literal measured 1.4:1 on the light theme.
//
// DEBUG's t3 is 4.3 on dark, marginally under the 4.5 this pass applied to INFO, and stays: t3 is the
// panel's tertiary token, and this pane already renders every line's timestamp and logger in it (:42,
// :44), so the four-letter marker is no dimmer than the columns beside it and raising it alone would
// only make it inconsistent. The line's own message is `text-t1` (18.0 / 13.7), so nothing a DEBUG line
// actually says is at that ratio. Raising t3 itself is a design-system change, not one to this map.
const LEVEL_TONE: Record<string, string> = { ERROR: "text-bad", WARNING: "text-warn", INFO: "text-t2", DEBUG: "text-t3" };

/** One rendered line. An ERROR line is tinted whole and bar-marked; a WARNING tints only its level. */
function LogLine({ line, query, phone }: { line: string; query: string; phone: boolean }) {
  const parsed = parseLogLine(line);
  const level = parsed?.level ?? "";
  const highlighted = (text: string) =>
    highlightParts(text, query).map((part, index) =>
      part.match ? <mark key={index} className="rounded bg-warn/25 px-0.5 text-warn">{part.text}</mark> : <span key={index}>{part.text}</span>,
    );
  return (
    <div className={cn("py-px", level === "ERROR" && "-mx-2 border-l-2 border-bad bg-bad/10 px-2")}>
      {parsed ? (
        <>
          <span className="text-t3">{phone ? shortTime(parsed.time) : parsed.time}</span>{" "}
          <span className={cn("font-semibold", LEVEL_TONE[level] ?? "text-t2")}>{level}</span>{" "}
          <span className="text-t3">{phone ? shortLogger(parsed.logger) : parsed.logger}</span>{" "}
          <span className={level === "ERROR" ? "text-bad" : "text-t1"}>{highlighted(parsed.message)}</span>
        </>
      ) : (
        <span className="text-t1">{highlighted(line)}</span>
      )}
    </div>
  );
}

/** G9's state. The filter is never part of the query key: it is applied at render and never sent to the gateway. */
export function useLogs() {
  const [source, setSource] = useState(DEFAULT_LOG_SOURCE);
  const [lines, setLines] = useState(String(DEFAULT_LOG_LINES));
  const [filter, setFilter] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const count = Math.min(MAX_LOG_LINES, Math.max(MIN_LOG_LINES, Math.trunc(Number(lines)) || DEFAULT_LOG_LINES));
  // One owner, and only while auto-refresh is on: an interval of 0 arms no timer. Changing the source or the count
  // makes a new key, so the previous one is left with no observer and drops — two owners are never armed at once.
  const query = usePolledQuery({ ...queries.logs(source, count), enabled: loaded }, autoRefresh && loaded ? LOGS_POLL_MS : 0);

  const all = query.data?.lines ?? [];
  const shown = filterLines(all, filter);

  function download() {
    // What the pane is showing, not everything that was loaded — the footer says so.
    downloadText(`${source}.log`, shown.join("\n"));
    notifyOk(`${source}.log downloaded · ${shown.length} lines`);
  }

  return {
    source, lines, filter, autoRefresh, loaded, count, query, all, shown, download,
    // Loaded, answered, and the answer holds nothing — the one case the empty copy speaks to.
    isEmpty: loaded && query.data !== undefined && all.length === 0,
    setSource: (next: string) => { setSource(next); setLoaded(false); },
    setLines: (next: string) => { setLines(next); setLoaded(false); },
    setFilter,
    setAutoRefresh,
    load: () => { setLoaded(true); if (loaded) void query.refetch(); },
  };
}

export type LogsState = ReturnType<typeof useLogs>;

export function LogPane({ logs, phone = false }: { logs: LogsState; phone?: boolean }) {
  const { loaded, shown, all, filter, source } = logs;
  const centred = "grid h-full place-items-center px-6 text-center font-sans text-[12px] leading-relaxed text-t3";
  return (
    <section aria-label="Log output" className="mt-3 max-h-[52vh] min-h-40 overflow-auto rounded-xl border border-line bg-glass px-2 py-1.5">
      {!loaded ? (
        <p className={centred}>{beforeLoadMessage(logs.count)}</p>
      ) : logs.isEmpty ? (
        <p className={centred}>{emptyMessage(source)}</p>
      ) : (
        <pre tabIndex={0} className="m-0 whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed focus-visible:outline-2 focus-visible:outline-g2">
          {shown.map((line, index) => <LogLine key={`${index}-${line}`} line={line} query={filter} phone={phone} />)}
        </pre>
      )}
      {loaded && shown.length === 0 && all.length > 0 ? (
        <p className={centred}>No loaded line contains “{filter.trim()}”.</p>
      ) : null}
    </section>
  );
}

export function LogFooter({ logs }: { logs: LogsState }) {
  const origin = logSource(logs.source)?.origin ?? "";
  const note = truncationNote(logs.source);
  return (
    <>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-t3">
        <span>{showingLabel(logs.shown.length, logs.all.length, logs.filter)}</span>
        <span>source <span className="font-semibold text-t2">{logSourceLabel(logs.source)}</span> · <span className="font-mono">{origin}</span></span>
        <span>{downloadNote(logs.shown.length, logs.all.length)}</span>
      </div>
      {note ? <p className="mt-1 text-[11px] text-t3">{note}</p> : null}
      <p className="mt-1 text-[11px] leading-relaxed text-t3">{LOGS_REDACTION_NOTE}</p>
    </>
  );
}

/** The phone's "Source & filter" sheet: every source with its annotation, the line count, the filter and Load. */
export function LogsSheet({ logs, open, onOpenChange }: { logs: LogsState; open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title="Source & filter" initialFocus="overlay">
        <div className="flex flex-col gap-3">
          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-xs font-semibold text-t2">Log source</legend>
            {LOG_SOURCES.map((entry) => (
              <label
                key={entry.value}
                className={cn(
                  "flex min-h-11 cursor-pointer items-center justify-between gap-2 rounded-xl border border-line px-3 text-[13px]",
                  "has-[:checked]:bg-glass-2 has-[:checked]:shadow-[inset_0_0_0_1px_var(--line)]",
                  entry.emptyByConfig ? "text-t3" : "text-t1",
                )}
              >
                <span className="flex items-center gap-2">
                  <input type="radio" name="phone-log-source" value={entry.value} checked={logs.source === entry.value} onChange={() => logs.setSource(entry.value)} className="sr-only" />
                  {entry.label}
                </span>
                <span className="truncate font-mono text-[11px] text-t3">{entry.emptyByConfig ? "empty by configuration" : entry.origin}</span>
              </label>
            ))}
          </fieldset>
          <p className="text-[11px] leading-relaxed text-t3">xray is not configured to write either file — its output is in xray output.</p>
          <div className="flex items-end gap-3">
            <label className="flex flex-1 flex-col gap-1 text-xs font-semibold text-t2">
              Lines
              <Input type="number" inputMode="numeric" min={MIN_LOG_LINES} max={MAX_LOG_LINES} value={logs.lines} onChange={(event) => logs.setLines(event.target.value)} />
            </label>
            <span className="flex items-center gap-2 pb-2.5 text-xs font-semibold text-t2">
              <Toggle label="Auto-refresh" checked={logs.autoRefresh} onCheckedChange={logs.setAutoRefresh} />
              every 5 s
            </span>
          </div>
          <label className="flex flex-col gap-1 text-xs font-semibold text-t2">
            Filter
            <Input type="search" placeholder="filter…" value={logs.filter} onChange={(event) => logs.setFilter(event.target.value)} />
          </label>
          <p className="-mt-1 text-[11px] text-t3">applied to the lines already loaded · not sent to the gateway</p>
          <div className="flex gap-2">
            <Button className="flex-1" disabled={!logs.filter} onClick={() => logs.setFilter("")}>Clear filter</Button>
            <Button className="flex-1" disabled={logs.shown.length === 0} onClick={logs.download}>Download shown</Button>
          </div>
          <div className="glass sticky bottom-0 -mx-1 flex bg-solid p-2">
            <Button variant="primary" className="flex-1" onClick={() => { logs.load(); onOpenChange(false); }}>Load</Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function LogsPhone({ logs }: { logs: LogsState }) {
  const [sheet, setSheet] = useState(false);
  return (
    <GlassCard aria-label="Logs">
      <div className="flex flex-wrap items-center gap-1.5">
        <Chip plain>{logSourceLabel(logs.source)}</Chip>
        <Chip plain>{logs.count} lines</Chip>
        {logs.filter.trim() ? <Chip plain>{logs.filter.trim()}</Chip> : null}
        <Button size="icon" variant="ghost" className="ml-auto" title="Source, lines, filter" aria-label="Source, lines, filter" onClick={() => setSheet(true)}>⋯</Button>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <Button variant="primary" className="flex-1" onClick={logs.load}>{logs.query.isFetching ? "Loading…" : "Load"}</Button>
        <Button className="flex-1" disabled={logs.shown.length === 0} onClick={logs.download}>Download</Button>
        {logs.autoRefresh ? <Chip tone="ok">5 s</Chip> : null}
      </div>
      {logs.query.isError ? (
        <div className="mt-3">
          <ErrorState message={`Could not read the log — ${logs.query.error.message}`} onRetry={() => void logs.query.refetch()} />
        </div>
      ) : null}
      <LogPane logs={logs} phone />
      <LogFooter logs={logs} />
      <LogsSheet logs={logs} open={sheet} onOpenChange={setSheet} />
    </GlassCard>
  );
}

/** System › Logs (G9). Nothing here is a write: no mutation key, no invalidation, and nothing disables on a busy flag. */
export function Logs() {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const logs = useLogs();
  if (!desktop) return <LogsPhone logs={logs} />;
  return (
    <GlassCard aria-label="Logs">
      <CardHeader
        title="Logs"
        detail={<Chip plain>on demand · nothing here is a write</Chip>}
        aside={<Chip tone={logs.autoRefresh ? "ok" : "neutral"}>{logs.autoRefresh ? "auto-refresh · every 5 s" : "paused"}</Chip>}
      />
      <div className="flex flex-wrap items-end gap-2.5">
        <SegmentedField
          legend="Log source"
          options={LOG_SOURCES.map((entry) => ({ value: entry.value, label: entry.label }))}
          value={logs.source}
          onValueChange={logs.setSource}
        />
        <label className="flex flex-col gap-1 text-xs font-semibold text-t2">
          lines
          <Input
            type="number" inputMode="numeric" min={MIN_LOG_LINES} max={MAX_LOG_LINES} className="w-24"
            value={logs.lines} onChange={(event) => logs.setLines(event.target.value)}
          />
        </label>
        <span className="pb-2 text-[11px] text-t3">1–1000</span>
        <Button variant="primary" onClick={logs.load}>{logs.query.isFetching && !logs.autoRefresh ? "Loading…" : "Load"}</Button>
        <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs font-semibold text-t2">
          filter
          <Input type="search" placeholder="filter…" value={logs.filter} onChange={(event) => logs.setFilter(event.target.value)} />
        </label>
        <span className="flex items-center gap-2 pb-2 text-xs font-semibold text-t2">
          <Toggle label="Auto-refresh" checked={logs.autoRefresh} onCheckedChange={logs.setAutoRefresh} />
          Auto-refresh
        </span>
        <Button className="mb-0" disabled={logs.shown.length === 0} onClick={logs.download}>Download</Button>
      </div>
      <p className="mt-2 text-[11px] leading-relaxed text-t3">{autoRefreshNote()}</p>
      <p className="mt-3 rounded-xl border border-line bg-glass px-3 py-2 text-[11.5px] leading-relaxed text-t2">{LOGS_EXPLAINER}</p>
      {logs.query.isError ? (
        <div className="mt-3">
          <ErrorState message={`Could not read the log — ${logs.query.error.message}`} onRetry={() => void logs.query.refetch()} />
        </div>
      ) : null}
      <LogPane logs={logs} />
      <LogFooter logs={logs} />
    </GlassCard>
  );
}
