import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { ApiError, api, isNoAnswer, type BackupFile, type RestoreResult, type Settings, type UndoResult } from "../../api/client";
import {
  CONNECTION_BUSY, RESTORE_WRITE, isConnectionBusy, isSettingsBusy, settingsWriteKey, useApiWrite, useConnectionBusy, useSettingsBusy,
} from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { usePolledQuery } from "../../api/live";
import { SLOW_POLL_MS } from "../../api/cadence";
import { useNow } from "../../components/data/Ago";
import { AlertBanner } from "../../components/data/AlertBanner";
import { CardHeader } from "../../components/data/CardHeader";
import { cardFallback } from "../../components/data/CardState";
import { Chip } from "../../components/data/Chip";
import { Elapsed } from "../../components/data/Elapsed";
import { KeyValueRows } from "../../components/data/KeyValueRows";
import { confirm } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { GlassCard } from "../../components/ui/GlassCard";
import { Pill } from "../../components/ui/Pill";
import { Toggle } from "../../components/ui/Toggle";
import { notifyError, notifyOk, notifyWarn } from "../../components/ui/Toaster";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { downloadText } from "../../lib/download";
import { fmtBytes } from "../../lib/format";
import { DESKTOP_QUERY, useMediaQuery } from "../../lib/media";
import { cn } from "../../lib/cn";
import { NO_ANSWER } from "../gateway/networkForm";
import { EditorSection } from "../tunnel/EditorSection";
import {
  BACKUP_CAPS, KIND_LABEL, NO_STORED, RESTORE_SENTENCE, STORED_NOTE, backupFailedMessage, backupFilename, backupPreChecks,
  backupWhen, checksPass, condensedBackupChecks, fileTooLarge, newestPreRestore, restoreConfirm, restoreRefusedMessage,
  restoredMessage, snapshotNote, undoConfirm, type PreCheck,
} from "./backupFile";
import { FilePicker } from "./FilePicker";
import { recordLastRestore, useLastRestore } from "./lastRestore";

const CREATE_HELPER = "the file is built and handed straight to the browser, never kept";
const RESTORE_HELPER =
  "Checked here only so an obvious mistake gets a sentence instead of a refusal. A file with no nodes at all is valid " +
  "and is sent. Everything else — references, setting bounds, the DHCP range and the lockout guard — is refused by the " +
  "gateway while it is still serving.";
const RESTORE_PROGRESS_HINT =
  "It refuses everything it can while the gateway is still serving, then stops xray, installs the fail-closed guard " +
  "and re-provisions the host. The request waits up to 180 s, and Connect stays blocked everywhere until it answers.";
const BACKUP_NOTE =
  "One JSON file with every node, subscription, anti-DPI profile, routing rule and panel setting. The Reality private " +
  "key and the remote-access client list are deliberately left out — paste the key again after restoring onto a new host.";
/** The tail of `AUTO_BACKUP_TEXT`, kept apart so the phone's collapsed-section body can share it word for word. */
const AUTO_BACKUP_STAYS_NOTE = "They stay on the box \u2014 download one from \u201cOn the gateway\u201d to take a copy off it.";
const AUTO_BACKUP_TEXT = `Keep a daily copy on the gateway (data/backups, the newest 7). ${AUTO_BACKUP_STAYS_NOTE}`;
const AUTO_BACKUP_ON_NOTE = "The first copy lands at the next daily run, not now.";
const FILE_HOLDS_NOTE =
  "It carries subscription URLs (which can embed a token), every node uuid, the Reality public key and short id, and " +
  "the LAN host map. Keep the file somewhere you would keep a password. It does not carry the panel password, the " +
  "session secret or any API token.";
const FILE_HOLDS_HINT = 'A hand-taken backup has no "taken at" stamp — only the daily job and the pre-restore snapshot write one.';

/** One pre-check, drawn like a CheckLine but never a live region: these change as the file is picked, not as work runs. */
export function CheckRow({ check }: { check: PreCheck }) {
  return (
    <li className="flex items-start gap-2 text-[12px] leading-relaxed">
      <span aria-hidden className={cn("mt-px font-bold", check.ok ? "text-ok" : "text-bad")}>{check.ok ? "✓" : "✕"}</span>
      <span className={check.ok ? "text-t2" : "text-bad"}>
        <span className="sr-only">{check.ok ? "passed: " : "failed: "}</span>
        {check.label}
      </span>
    </li>
  );
}

/**
 * P1: a read with effects, guarded by a local flag — never a mutation key, because it changes nothing. `filename`
 * defaults to a fresh `backupFilename(new Date())` for a caller with no render-time clock of its own, but the
 * desktop card passes the same value it renders as the "named …" label, through `useNow`, so the two can never
 * disagree across a midnight rollover.
 */
export function useCreateBackup() {
  const [preparing, setPreparing] = useState(false);
  async function create(filename: string = backupFilename(new Date())) {
    if (preparing) return;
    setPreparing(true);
    try {
      // Fetched on click, stringified, handed to the browser and dropped: no useQuery, no cache, no kept reference.
      const doc = await api.getBackup();
      downloadText(filename, JSON.stringify(doc, null, 2), "application/json");
      notifyOk(`backup downloaded · ${filename}`);
    } catch (error) {
      notifyError(null, backupFailedMessage(error));
    } finally {
      setPreparing(false);
    }
  }
  return { preparing, create };
}

/** A restore and its undo fail in exactly the same ways — one handler, so the two can never drift apart. */
function notifyRestoreError(queryClient: QueryClient, error: Error): void {
  if (isNoAnswer(error)) {
    // The gateway keeps working under its lock: this may still have replaced everything.
    notifyWarn(NO_ANSWER);
    void queryClient.invalidateQueries();
    return;
  }
  if (!(error instanceof ApiError)) {
    notifyError(error, "not restored");
    return;
  }
  const refusal = restoreRefusedMessage(error);
  notifyError(null, refusal.message, { sticky: refusal.sticky });
}


interface PickedFile {
  file: File;
  text: string;
}

interface RestoreVariables {
  filename: string;
}

/**
 * P2: the picked file, its checks, and the write that replaces the whole configuration with it.
 *
 * Deviation from the brief (folded in from the Task 7 review): the brief's `mutation.mutate({ doc, filename })`
 * makes the whole parsed document a mutation variable. TanStack keeps a settled mutation's `state.variables` in the
 * MutationCache for its gcTime (default 5 min) — after success and after a failed attempt alike — so the document
 * would sit there reachable long after `picked` cleared it, against the "secrets/large payloads out of the cache"
 * constraint. The repo already paid for this once, for the remote-access private key (`useRwSave`) and the panel
 * password (`usePasswordChange`): the document travels through a ref instead (`pendingDoc`), read once by
 * `mutationFn` and cleared the moment it is, so the only thing `mutate()` ever hands TanStack is the filename.
 */
export function useRestore() {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<PickedFile | null>(null);
  const restore = useApiWrite("restore");
  const connectionBusy = useConnectionBusy();
  const pendingDoc = useRef<Record<string, unknown> | null>(null);

  const mutation = useMutation<RestoreResult, Error, RestoreVariables>({
    mutationKey: RESTORE_WRITE,
    mutationFn: () => {
      const doc = pendingDoc.current;
      pendingDoc.current = null;
      if (!doc) throw new Error("useRestore: restore fired with no pending document");
      return restore(doc);
    },
    onSuccess: (result, { filename }) => {
      recordLastRestore({ result, filename, at: Date.now() });
      setPicked(null);
      const { message, tone } = restoredMessage(result);
      if (tone === "warn") notifyWarn(message);
      else notifyOk(message);
    },
    onError: (error) => notifyRestoreError(queryClient, error),
  });

  async function pick(file: File | null) {
    if (!file) return void setPicked(null);
    const tooLarge = fileTooLarge(file.size);
    if (tooLarge) {
      setPicked({ file, text: "" });
      return;
    }
    try {
      setPicked({ file, text: await file.text() });
    } catch {
      // An evicted or moved file (NotReadableError, common on an iCloud-synced tree) must not vanish silently
      // in the screen's most destructive flow: say so, and leave no stale file behind for a wrong Restore to fire on.
      setPicked(null);
      notifyError(null, "could not read that file");
    }
  }

  const checks = picked ? backupPreChecks(picked.text, picked.file.size) : [];

  /** Sends, having already asked — the phone sheet IS the question, so it calls this instead of `send`. */
  function run() {
    if (!picked || !checksPass(checks) || mutation.isPending) return;
    // The question was open for as long as it took to answer: another connection write may have started.
    if (isConnectionBusy(queryClient)) return void notifyError(null, CONNECTION_BUSY);
    let doc: Record<string, unknown>;
    try {
      doc = JSON.parse(picked.text) as Record<string, unknown>;
    } catch {
      return void notifyError(null, "not restored — not a valid backup file");
    }
    pendingDoc.current = doc;
    mutation.mutate({ filename: picked.file.name });
  }

  async function send() {
    if (!picked || !checksPass(checks) || mutation.isPending) return;
    if (!(await confirm(restoreConfirm(picked.file.name, picked.file.size), { confirmLabel: "Restore" }))) return;
    run();
  }

  return {
    picked, checks, send, run, mutation,
    condensed: picked ? condensedBackupChecks(picked.text, picked.file.size) : [],
    tooLarge: picked ? fileTooLarge(picked.file.size) : null,
    clear: () => setPicked(null),
    pick,
    busy: mutation.isPending || connectionBusy,
  };
}

export type RestoreState = ReturnType<typeof useRestore>;

/** G5, the backup half: saved the moment it is flipped, optimistic with a revert. Not a re-apply key. */
export function useAutoBackup() {
  const queryClient = useQueryClient();
  // This route's polling owner for `settings`; the three System screens are separate routes and never mount together.
  const settings = usePolledQuery(queries.settings(), SLOW_POLL_MS);
  const putSettings = useApiWrite("putSettings");
  const settingsBusy = useSettingsBusy();
  const connectionBusy = useConnectionBusy();
  const mutation = useMutation({
    mutationKey: settingsWriteKey({ auto_backup_enabled: true }),
    mutationFn: (on: boolean) => putSettings({ auto_backup_enabled: on }),
    onMutate: (on) => {
      const before = queryClient.getQueryData<Settings>(keys.settings)?.auto_backup_enabled;
      queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, auto_backup_enabled: on } : old));
      return { before };
    },
    onSuccess: (_saved, on) => notifyOk(on ? "daily auto-backup on" : "daily auto-backup off"),
    onError: (error, _on, context) => {
      if (context?.before !== undefined) {
        const before = context.before;
        queryClient.setQueryData<Settings>(keys.settings, (old) => (old ? { ...old, auto_backup_enabled: before } : old));
      }
      if (isNoAnswer(error)) notifyWarn(NO_ANSWER);
      else notifyError(error, "daily auto-backup was not saved");
      void queryClient.invalidateQueries({ queryKey: keys.settings });
    },
  });

  function flip(on: boolean) {
    if (isSettingsBusy(queryClient) || isConnectionBusy(queryClient)) return;
    mutation.mutate(on);
  }

  return { settings, busy: settingsBusy || connectionBusy, flip, on: settings.data?.auto_backup_enabled ?? false };
}

export function BackupCard({ create }: { create: ReturnType<typeof useCreateBackup> }) {
  const connectionBusy = useConnectionBusy();
  // One clock read, through useNow rather than a bare `new Date()` in render, so the name shown here and the
  // name the click handler downloads under are always the same value — never two reads that can straddle midnight.
  const now = useNow();
  const filename = backupFilename(new Date(now));
  return (
    <GlassCard aria-label="Backup & restore">
      <CardHeader title="Backup & restore" aside={<Chip plain>one JSON file</Chip>} />
      <p className="text-xs leading-relaxed text-t2">{BACKUP_NOTE}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button variant="primary" disabled={create.preparing || connectionBusy} onClick={() => void create.create(filename)}>
          {create.preparing ? "Preparing…" : "Create backup"}
        </Button>
        <p className="min-w-0 text-[11px] text-t3">
          named <span className="font-mono text-t2">{filename}</span> · {CREATE_HELPER}
        </p>
      </div>
    </GlassCard>
  );
}

/** The progress row for a restore in flight — a pulsing bar named `Restoring` plus an elapsed clock. Shared by the
 * desktop card and the phone sheet's footer, so the two never drift into two different renderings of the same state. */
function RestoreProgress({ submittedAt }: { submittedAt: number }) {
  return (
    <>
      <div role="progressbar" aria-label="Restoring" className="h-1 min-w-24 flex-1 overflow-hidden rounded-full bg-glass-2">
        <span className="block h-full w-1/3 animate-pulse rounded-full bg-brand" />
      </div>
      <span className="font-mono text-[11px] text-t2"><Elapsed since={submittedAt} format="clock" /></span>
    </>
  );
}

export function RestoreCard({ restore }: { restore: RestoreState }) {
  const { picked, checks, mutation } = restore;
  const running = mutation.isPending;
  return (
    <GlassCard aria-label="Restore from file">
      <CardHeader title="Restore from file" aside={<Chip tone="bad" plain>replaces everything · disconnects the gateway</Chip>} />
      <FilePicker label="Choose file…" file={picked?.file ?? null} onPick={(file) => void restore.pick(file)} disabled={restore.busy} />
      {restore.tooLarge ? (
        <AlertBanner tone="bad" className="mt-3" title={restore.tooLarge.title} text={restore.tooLarge.text} />
      ) : picked ? (
        <>
          <ul className="mt-3 flex flex-col gap-1">
            {checks.map((check) => <CheckRow key={check.label} check={check} />)}
          </ul>
          <p className="mt-2 text-[11px] leading-relaxed text-t3">{RESTORE_HELPER}</p>
        </>
      ) : null}
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        {running ? (
          <RestoreProgress submittedAt={mutation.submittedAt} />
        ) : (
          <p className="min-w-0 flex-1 text-[11px] text-t3">nothing is written until you confirm</p>
        )}
        <Button disabled={!picked || restore.busy} onClick={restore.clear}>Clear</Button>
        <Button
          variant="danger"
          disabled={!picked || !checksPass(checks) || restore.busy}
          onClick={() => void restore.send()}
        >
          {running ? "Restoring…" : "Restore"}
        </Button>
      </div>
      {running ? <p className="mt-2 text-[11px] leading-relaxed text-t3">{RESTORE_PROGRESS_HINT}</p> : null}
    </GlassCard>
  );
}

export function LastRestoreCard({ phone = false }: { phone?: boolean }) {
  const last = useLastRestore();
  if (!last) return null;
  const { restored } = last.result;
  const time = new Date(last.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <GlassCard aria-label="Last restore" className="border-ok/40">
      <CardHeader
        title="Last restore"
        detail={`${time} · from ${last.filename}`}
        aside={<Pill tone="bad">{phone ? "disconnected" : "runtime: disconnected"}</Pill>}
      />
      <KeyValueRows
        className="sm:grid-cols-4"
        rows={[
          { key: "NODES", value: restored.nodes },
          { key: "SUBSCRIPTIONS", value: restored.subscriptions },
          { key: phone ? "PROFILES" : "ANTI-DPI PROFILES", value: restored.profiles },
          { key: "ROUTING RULES", value: restored.routing_rules },
        ]}
      />
      {restored.rw_disabled ? (
        <AlertBanner tone="warn" className="mt-3" title="Remote access was turned off" text={restored.rw_disabled} />
      ) : null}
      <p className="mt-3 break-all text-[11px] leading-relaxed text-t3">{snapshotNote(last.result)}</p>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
        <p className="min-w-0 text-[11px] text-t3">The gateway is disconnected — Connect a node when you are ready.</p>
        <Button asChild variant="primary"><Link to="/">{phone ? "Go to Home and connect" : "Go to Home"}</Link></Button>
      </div>
    </GlassCard>
  );
}

export function AutoBackupCard({ auto }: { auto: ReturnType<typeof useAutoBackup> }) {
  const fallback = cardFallback([auto.settings], "Settings did not load", "h-16");
  return (
    <GlassCard aria-label="Daily auto-backup">
      <CardHeader title="Daily auto-backup" aside={<Chip plain>stays on the gateway</Chip>} />
      {fallback ?? (
        <>
          <div className="flex items-start gap-3">
            <div className="pt-0.5">
              <AutoBackupSwitch auto={auto} />
            </div>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-t1">Daily auto-backup</p>
              <p className="text-[11.5px] leading-relaxed text-t3">{AUTO_BACKUP_TEXT}</p>
            </div>
          </div>
          {auto.on ? (
            <p className="mt-3 rounded-xl border border-line bg-glass px-3 py-2 text-[11.5px] text-t2">{AUTO_BACKUP_ON_NOTE}</p>
          ) : null}
        </>
      )}
    </GlassCard>
  );
}

/** Kept apart so a phone can put it in an EditorSection's aside slot, never nested inside the header button. */
export function AutoBackupSwitch({ auto }: { auto: ReturnType<typeof useAutoBackup> }) {
  return (
    <Toggle label="Daily auto-backup" checked={auto.on} disabled={auto.busy || !auto.settings.data} onCheckedChange={auto.flip} />
  );
}

/**
 * A8: the copies the gateway already keeps — the daily job's, and the one every restore takes of what it is
 * about to replace. Both existed before this card; neither was readable from the panel, so the safety net was
 * invisible and an undo meant finding the file over ssh.
 */
export function useStoredBackups() {
  const queryClient = useQueryClient();
  const files = useQuery(queries.backups());       // read once: these change only when a backup or a restore runs
  const [downloading, setDownloading] = useState("");
  const connectionBusy = useConnectionBusy();
  const undoRestore = useApiWrite("undoRestore");

  const undo = useMutation<UndoResult, Error, BackupFile>({
    // The same key a restore writes under: it IS one, so the two block each other everywhere.
    mutationKey: RESTORE_WRITE,
    mutationFn: () => undoRestore(),
    onSuccess: (result, file) => {
      recordLastRestore({ result, filename: file.name, at: Date.now() });
      const { message, tone } = restoredMessage(result);
      if (tone === "warn") notifyWarn(message);
      else notifyOk(message);
    },
    onError: (error) => notifyRestoreError(queryClient, error),
  });

  async function download(file: BackupFile) {
    if (downloading) return;
    setDownloading(file.name);
    try {
      // Fetched on click, handed to the browser and dropped — same as Create backup, no cache, no kept reference.
      downloadText(file.name, JSON.stringify(await api.getStoredBackup(file.name), null, 2), "application/json");
      notifyOk(`downloaded · ${file.name}`);
    } catch (error) {
      notifyError(null, backupFailedMessage(error));
    } finally {
      setDownloading("");
    }
  }

  async function askUndo(file: BackupFile) {
    if (undo.isPending || connectionBusy) return;
    if (!(await confirm(undoConfirm(file), { confirmLabel: "Undo restore" }))) return;
    if (isConnectionBusy(queryClient)) return void notifyError(null, CONNECTION_BUSY);
    undo.mutate(file);
  }

  return { files, download, downloading, askUndo, undo, busy: undo.isPending || connectionBusy };
}

export type StoredBackupsState = ReturnType<typeof useStoredBackups>;

function StoredRow({ file, stored }: { file: BackupFile; stored: StoredBackupsState }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1.5 text-[13px]">
      <span className="w-32 shrink-0 text-t2">{backupWhen(file.created_at)}</span>
      <span className="min-w-0 flex-1 truncate text-t3">{KIND_LABEL[file.kind]} · {fmtBytes(file.bytes)}</span>
      <Button
        className="h-7 px-2 text-[11px]"
        disabled={stored.downloading !== ""}
        onClick={() => void stored.download(file)}
      >
        {stored.downloading === file.name ? "…" : "Download"}
      </Button>
    </li>
  );
}

export function StoredBackupsCard({ stored }: { stored: StoredBackupsState }) {
  const files = stored.files.data ?? [];
  const newest = newestPreRestore(files);
  return (
    <GlassCard aria-label="On the gateway">
      <CardHeader
        title="On the gateway"
        detail={stored.files.data ? `${files.length} kept` : "data/backups"}
        aside={<Chip plain>data/backups</Chip>}
      />
      {cardFallback([stored.files], "the stored copies did not load", "h-20") ?? (
        files.length === 0 ? (
          <p className="text-sm text-t3">{NO_STORED}</p>
        ) : (
          <ul aria-label="Stored backups" className="flex flex-col divide-y divide-line">
            {files.slice(0, 10).map((file) => <StoredRow key={file.name} file={file} stored={stored} />)}
          </ul>
        )
      )}
      {newest ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
          <p className="min-w-0 flex-1 text-[11px] text-t3">
            The last restore saved what it replaced — it can be put back.
          </p>
          <Button variant="danger" disabled={stored.busy} onClick={() => void stored.askUndo(newest)}>
            {stored.undo.isPending ? "Undoing…" : "Undo last restore"}
          </Button>
        </div>
      ) : null}
      <p className="mt-2 text-[11px] leading-relaxed text-t3">{STORED_NOTE}</p>
    </GlassCard>
  );
}


export function FileHoldsCard() {
  const nodes = useQuery(queries.nodes());
  const subs = useQuery(queries.subs());
  const profiles = useQuery(queries.profiles());
  const routing = useQuery(queries.routing());
  const count = (value: number | undefined) => (value === undefined ? "—" : value);
  return (
    <GlassCard aria-label="What the file holds">
      <CardHeader title="What the file holds" detail="schema 2" />
      <KeyValueRows
        rows={[
          { key: "nodes", value: count(nodes.data?.length), sub: `of at most ${BACKUP_CAPS.nodes}` },
          { key: "subscriptions", value: count(subs.data?.length), sub: `of at most ${BACKUP_CAPS.subscriptions}` },
          { key: "profiles", value: count(profiles.data?.length), sub: `of at most ${BACKUP_CAPS.profiles}` },
          { key: "routing", value: routing.data ? `${routing.data.rules.length} rules + default action` : "—", sub: `of at most ${BACKUP_CAPS.rules} rules` },
          // The client cannot know the file's settings count: the backup's allowlist is not SettingsOut, so a
          // number taken from the cached settings would be a different one wearing this file's label.
          { key: "settings", value: "every panel setting", sub: `at most ${BACKUP_CAPS.settings} keys` },
        ]}
      />
      <p className="mt-3 text-[11px] leading-relaxed text-t2">{FILE_HOLDS_NOTE}</p>
      <p className="mt-2 text-[11px] leading-relaxed text-t3">{FILE_HOLDS_HINT}</p>
    </GlassCard>
  );
}

/**
 * The restore question on a phone: the file, its checks and the sentence in one place, over its own sticky footer.
 *
 * Deviation from the brief (review fix round 1): the brief's Restore button closes the sheet in the same click that
 * starts `run()` (`onClick={() => { restore.run(); onOpenChange(false); }}`), which makes `{restore.mutation.isPending
 * ? "Restoring…" : "Restore"}` dead code — the sheet is gone in the render where `isPending` first turns true, and a
 * 180 s destructive write is left with no visible indicator once the "Daily auto-backup" section is opened, hiding
 * the accordion's own "Restoring…" label. The sheet now stays mounted and open for as long as the mutation is
 * pending — its own effect below closes it only once the mutation settles, matching mockup
 * `16-approved-system-backups.html`'s own caption that "Restoring…" stays in this same sheet. `Cancel` and the
 * outer Escape/overlay-click path are both blocked while pending too, for the same reason `RestoreCard`'s `Clear`
 * is disabled while busy: a running write must not be hideable back into invisibility by any of the sheet's own
 * closing gestures.
 */
export function RestoreSheet({ restore, open, onOpenChange }: { restore: RestoreState; open: boolean; onOpenChange: (open: boolean) => void }) {
  const pending = restore.mutation.isPending;
  const wasPending = useRef(false);
  useEffect(() => {
    if (wasPending.current && !pending) onOpenChange(false);
    wasPending.current = pending;
  }, [pending, onOpenChange]);

  return (
    <Sheet open={open} onOpenChange={(next) => { if (!next && pending) return; onOpenChange(next); }}>
      <SheetContent title="Replace everything?" initialFocus="overlay">
        <div className="flex flex-col gap-3">
          <Pill tone="bad" className="self-start">Restore</Pill>
          <FilePicker label="Choose file…" file={restore.picked?.file ?? null} onPick={(file) => void restore.pick(file)} disabled={restore.busy} />
          <ul className="flex flex-col gap-1">
            {restore.condensed.map((check) => <CheckRow key={check.label} check={check} />)}
          </ul>
          <p className="text-[11.5px] leading-relaxed text-t2">{RESTORE_SENTENCE} Continue?</p>
          <p className="text-[11px] leading-relaxed text-t3">
            A copy of what this replaces is saved on the gateway first. It can take up to three minutes, and Connect
            stays blocked everywhere until it answers.
          </p>
          <div className="glass sticky bottom-0 -mx-1 flex flex-wrap items-center gap-2 bg-solid p-2">
            {pending ? <RestoreProgress submittedAt={restore.mutation.submittedAt} /> : null}
            <Button className="flex-1" disabled={restore.busy} onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button
              variant="danger"
              className="flex-1"
              disabled={!restore.picked || !checksPass(restore.checks) || restore.busy}
              onClick={() => restore.run()}
            >
              {pending ? "Restoring…" : "Restore"}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function BackupsPhone({ create, restore, auto, stored }: { create: ReturnType<typeof useCreateBackup>; restore: RestoreState; auto: ReturnType<typeof useAutoBackup>; stored: StoredBackupsState }) {
  const [open, setOpen] = useState<"restore" | "auto" | null>("restore");
  const [sheet, setSheet] = useState(false);
  return (
    <div className="flex flex-col gap-3">
      <GlassCard aria-label="Backup & restore">
        <CardHeader title="Backup & restore" aside={<Chip plain>one JSON file</Chip>} />
        <p className="text-xs leading-relaxed text-t2">{BACKUP_NOTE}</p>
        <Button variant="primary" className="mt-3 w-full" disabled={create.preparing || restore.busy} onClick={() => void create.create()}>
          {create.preparing ? "Preparing…" : "Create backup"}
        </Button>
      </GlassCard>
      <GlassCard>
        <EditorSection
          title="Restore from file"
          collapsible
          open={open === "restore"}
          onToggle={() => setOpen((current) => (current === "restore" ? null : "restore"))}
          summary="replaces everything"
        >
          <FilePicker label="Choose file…" file={restore.picked?.file ?? null} onPick={(file) => void restore.pick(file)} disabled={restore.busy} />
          {restore.tooLarge ? (
            <AlertBanner tone="bad" title={restore.tooLarge.title} text={restore.tooLarge.text} />
          ) : restore.picked ? (
            <ul className="flex flex-col gap-1">
              {restore.condensed.map((check) => <CheckRow key={check.label} check={check} />)}
            </ul>
          ) : null}
          <Button
            variant="danger"
            className="w-full"
            disabled={!restore.picked || !checksPass(restore.checks) || restore.busy}
            onClick={() => setSheet(true)}
          >
            {restore.mutation.isPending ? "Restoring…" : "Restore"}
          </Button>
        </EditorSection>
        <EditorSection
          title="Daily auto-backup"
          note="data/backups · the newest 7"
          collapsible
          open={open === "auto"}
          onToggle={() => setOpen((current) => (current === "auto" ? null : "auto"))}
          // Beside the header button, never inside it: no nested interactive controls.
          aside={<AutoBackupSwitch auto={auto} />}
        >
          <p className="text-[11.5px] leading-relaxed text-t3">
            {AUTO_BACKUP_STAYS_NOTE} {AUTO_BACKUP_ON_NOTE}
          </p>
        </EditorSection>
      </GlassCard>
      <LastRestoreCard phone />
      <StoredBackupsCard stored={stored} />
      <RestoreSheet restore={restore} open={sheet} onOpenChange={setSheet} />
    </div>
  );
}

/** System › Backups (P1, P2 and G5's daily-copy half). `settings` is this route's polling owner at 30 s. */
export function Backups() {
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const create = useCreateBackup();
  const restore = useRestore();
  const auto = useAutoBackup();
  const stored = useStoredBackups();
  if (!desktop) return <BackupsPhone create={create} restore={restore} auto={auto} stored={stored} />;
  return (
    <div className="grid gap-3 md:grid-cols-[7fr_5fr] md:items-start">
      <div className="flex flex-col gap-3">
        <BackupCard create={create} />
        <RestoreCard restore={restore} />
        <LastRestoreCard />
      </div>
      <div className="flex flex-col gap-3">
        <AutoBackupCard auto={auto} />
        <StoredBackupsCard stored={stored} />
        <FileHoldsCard />
      </div>
    </div>
  );
}
