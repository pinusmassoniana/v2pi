import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { ApiError, api, errText, type PresetInfo, type Routing, type RoutingIn, type Status } from "../../api/client";
import { CONNECTION_BUSY, ROUTING_WRITE, invalidate, isConnectionBusy, saveRefusedMessage, useApiWrite } from "../../api/invalidation";
import { keys, queries } from "../../api/keys";
import { confirm } from "../../components/confirm";
import type { ReadQuery } from "../../components/ui/States";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { copyText } from "../../lib/clipboard";
import { checkResult, runKey, type CheckResult } from "../../lib/staleResult";
import {
  applyPreset, BLOCK_DEFAULT_CONFIRM, datasetInstalled, DISCARD_STAGED_CONFIRM, exportJson, missingDatasetConfirm, RESET_CONFIRM, resetRouting, toRoutingIn, type StagedRouting,
} from "./rules";
import type { RoutingEditor } from "./useRoutingEditor";

/** R6 Save's message: whether the tunnel took it now, and how many duplicate rows were left out. */
export function routingSavedMessage(applied: boolean, dropped: number): string {
  return `${applied ? "saved & applied" : "saved — applies on next Connect"}${dropped > 0 ? ` · ${dropped} duplicate row(s) dropped` : ""}`;
}

interface SaveVariables {
  body: RoutingIn;
  dropped: number;
  /** A node was active when Save was pressed, so the gateway re-applies the tunnel now. */
  applied: boolean;
}

export interface RoutingActions {
  /** The last Validate result, keyed by the ruleset it checked. */
  check: CheckResult | null;
  validating: boolean;
  validate: () => Promise<void>;
  saving: boolean;
  /** A 422 from the last Save, shown under the staged banner. */
  saveError: string | null;
  save: () => Promise<void>;
  presets: readonly PresetInfo[] | undefined;
  /** The presets read itself, so a failure can be said where Import preset is. */
  presetsQuery: ReadQuery;
  presetBusy: boolean;
  stagePreset: (name: string) => Promise<void>;
  exportRules: () => Promise<void>;
  /** Replace the staged rules with an imported ruleset, asking first when something is staged. */
  importRules: (state: StagedRouting) => Promise<boolean>;
  reset: () => Promise<void>;
  discard: () => void;
}

/**
 * R4, R6, R7: Validate, Save, presets, JSON export / import and Reset over the editor. Validate and presets are peeks —
 * nothing is invalidated after them. Save always re-applies the tunnel, so it is a connection write (ROUTING_WRITE).
 */
export function useRoutingActions(editor: RoutingEditor): RoutingActions {
  const queryClient = useQueryClient();
  const presets = useQuery(queries.routingPresets());   // read once; nothing polls presets
  // A3: which geo datasets are actually installed, so a preset that needs the RU lists can say so
  // before it stages rules that would fail to apply. Read once; the Panel screen owns updates.
  const geo = useQuery(queries.geo());
  const putRouting = useApiWrite("putRouting");
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [presetBusy, setPresetBusy] = useState(false);
  const { current, staged, replace, discard: discardEdits } = editor;

  // What a save did is said even if the screen has gone; what it does to the editor is passed to mutate below.
  const saveMutation = useMutation({
    mutationKey: ROUTING_WRITE,
    mutationFn: ({ body }: SaveVariables) => putRouting(body),
    onSuccess: (_saved, { applied, dropped }) => notifyOk(routingSavedMessage(applied, dropped)),
    onError: (error) => {
      if (error instanceof ApiError && error.status === 502) notifyError(null, saveRefusedMessage(error));
      else if (!(error instanceof ApiError && error.status === 422)) notifyError(error, "save failed");
    },
  });

  const discard = useCallback(() => {
    setSaveError(null);
    discardEdits();
  }, [discardEdits]);

  async function validate() {
    if (!current) return;
    const { body } = toRoutingIn(current);
    const key = runKey(body);
    setValidating(true);
    try {
      setCheck(checkResult(await api.validateRouting(body), "ruleset valid", key));
    } catch (error) {
      setCheck({ ok: false, text: `✗ ${errText(error, "validate failed")}`, key });
    } finally {
      setValidating(false);
    }
  }

  async function save() {
    if (!current) return;
    if (current.defaultAction === "block") {
      if (!(await confirm(BLOCK_DEFAULT_CONFIRM, { confirmLabel: "Save" }))) return;
      // Another connection write may have started while the question was open.
      if (isConnectionBusy(queryClient)) {
        notifyError(null, CONNECTION_BUSY);
        return;
      }
    }
    const { body, dropped } = toRoutingIn(current);
    const applied = (queryClient.getQueryData<Status>(keys.status)?.active_node_id ?? null) !== null;
    setSaveError(null);
    saveMutation.mutate({ body, dropped, applied }, {
      onSuccess: (saved: Routing) => {
        queryClient.setQueryData(keys.routing, saved);
        discardEdits();
      },
      onError: (error) => {
        if (error instanceof ApiError && error.status === 422) setSaveError(error.message);
        // Not saved: the store rolled the write back, so the gateway's ruleset is unchanged — re-reading it is
        // harmless (and lets a gatewayChanged notice fire if something else moved it meanwhile), but the staged
        // edits stay so the operator can fix the rule and press Save again.
        if (error instanceof ApiError && error.status === 502) void invalidate(queryClient, "putRouting");
      },
    });
  }

  async function stagePreset(name: string) {
    const preset = presets.data?.find((item) => item.name === name);
    const missing = preset?.dataset ? !datasetInstalled(geo.data, preset.dataset) : false;
    if (missing && !(await confirm(missingDatasetConfirm(preset!.dataset), { confirmLabel: "Stage anyway" }))) return;
    if (staged && !(await confirm(DISCARD_STAGED_CONFIRM, { confirmLabel: "Discard" }))) return;
    setPresetBusy(true);
    try {
      replace(applyPreset(await api.routingPreset(name)));
      setSaveError(null);
      notifyOk(`preset "${name}" staged — review and Save`);
    } catch (error) {
      notifyError(error, "preset failed");
    } finally {
      setPresetBusy(false);
    }
  }

  async function exportRules() {
    if (!current) return;
    try {
      await copyText(exportJson(current));   // the panel is served over plain HTTP, where navigator.clipboard does not exist
      notifyOk("ruleset copied as JSON");
    } catch {
      notifyError(null, "copy failed");
    }
  }

  async function importRules(next: StagedRouting): Promise<boolean> {
    if (staged && !(await confirm(DISCARD_STAGED_CONFIRM, { confirmLabel: "Discard" }))) return false;
    replace(next);
    setSaveError(null);
    notifyOk("imported — review and Save");
    return true;
  }

  async function reset() {
    if (!(await confirm(RESET_CONFIRM, { confirmLabel: "Reset" }))) return;
    replace(resetRouting());
    setSaveError(null);
  }

  return {
    check, validating, validate, saving: saveMutation.isPending, saveError, save, presets: presets.data, presetsQuery: presets, presetBusy, stagePreset,
    exportRules, importRules, reset, discard,
  };
}
