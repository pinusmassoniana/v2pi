import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { ApiError, api, errText, type Node } from "../../api/client";
import { useApiWrite } from "../../api/invalidation";
import { queries } from "../../api/keys";
import { useUnsavedGuard } from "../../app/guard";
import { closeGuarded } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { FieldShell, SegmentedField, TextField } from "../../components/ui/Field";
import { ProfileSelect } from "../../components/ui/ProfileSelect";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { notifyError, notifyOk } from "../../components/ui/Toaster";
import { cn } from "../../lib/cn";
import { SERVERS } from "./list";
import {
  ACTIVE_NODE_MESSAGE, BLANK_NODE_FORM, IDENTITY_MESSAGE, MAX_FIELD, cloneToForm, formToNodeIn, formToNodeUpdate, formToValidate,
  isIdentityConflict, nodeFormSchema, nodeToForm, validateMessage, type NodeFormValues,
} from "./nodeForm";
import { useNodeConnection } from "./useNodeActions";

export type NodeFormMode = "add" | "clone" | "edit";

export interface NodeFormSheetProps {
  mode: NodeFormMode;
  /** The node to clone or edit. */
  node?: Node;
  onClose: () => void;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2.5 rounded-2xl border border-line bg-glass p-3">
      <h3 className="text-[10.5px] font-semibold uppercase tracking-[.07em] text-t3">{title}</h3>
      {children}
    </section>
  );
}

/** N13: the edit refused because the node is active — say so, and offer the Disconnect that unblocks it. */
function ActiveBanner({ node }: { node: Node }) {
  const connection = useNodeConnection(node, true);
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2.5 rounded-xl border border-warn/40 bg-warn/10 p-3 text-sm text-t1">
      <p className="min-w-0 flex-1">{ACTIVE_NODE_MESSAGE}</p>
      <Button size="sm" variant="danger" disabled={connection.disabled} title={connection.reason ?? undefined} onClick={connection.toggle}>{connection.label}</Button>
    </div>
  );
}

/**
 * N12 add, N14 clone (Add prefilled) and N13 edit. The form takes its values once, when it opens, and never reseeds
 * from a poll; closing with unsaved edits asks first (the sheet's `dirty` and the navigation blocker).
 */
export function NodeFormSheet({ mode, node, onClose }: NodeFormSheetProps) {
  const edit = mode === "edit" && node !== undefined;
  const navigate = useNavigate();
  const [defaults] = useState<NodeFormValues>(() => (edit ? nodeToForm(node) : mode === "clone" && node ? cloneToForm(node) : BLANK_NODE_FORM));
  const { register, handleSubmit, control, formState, getValues, trigger } = useForm<NodeFormValues>({
    resolver: zodResolver(nodeFormSchema),
    defaultValues: defaults,
  });
  const transport = useWatch({ control, name: "transport" });
  const security = useWatch({ control, name: "security" });
  const dirty = formState.isDirty;
  useUnsavedGuard(dirty);
  const profiles = useQuery({ ...queries.profiles(), enabled: edit });
  const [validation, setValidation] = useState<{ ok: boolean; text: string } | null>(null);
  const [validating, setValidating] = useState(false);
  const [conflict, setConflict] = useState<"active" | "identity" | null>(null);
  const addWrite = useApiWrite("addNode");
  const updateWrite = useApiWrite("updateNode");

  const save = useMutation({
    mutationFn: (values: NodeFormValues) => (edit ? updateWrite(node.id, formToNodeUpdate(values)) : addWrite(formToNodeIn(values))),
    onSuccess: (saved) => {
      notifyOk(edit ? `Saved ${saved.name}` : `Added ${saved.name}`);
      onClose();
      // N12: a new server is manual — show it in Servers. The form is closing, so nothing unsaved is left behind.
      if (!edit) void navigate({ to: "/nodes", search: { group: SERVERS }, ignoreBlocker: true });
    },
    onError: (error) => {
      // Add has one 409 (the identity); edit has two, told apart by the server's detail.
      if (error instanceof ApiError && error.status === 409) setConflict(edit && !isIdentityConflict(error) ? "active" : "identity");
      else notifyError(error, edit ? "save failed" : "add failed");
    },
  });

  async function validate() {
    const parsed = nodeFormSchema.safeParse(getValues());
    if (!parsed.success) {
      void trigger();
      setValidation({ ok: false, text: `✗ ${parsed.error.issues[0]!.message}` });
      return;
    }
    setValidating(true);
    try {
      // A peek: it checks the config with xray and changes nothing, so nothing is invalidated after it.
      setValidation(validateMessage(await api.validateNode(formToValidate(parsed.data, edit))));
    } catch (error) {
      setValidation({ ok: false, text: `✗ ${errText(error, "validate failed")}` });
    } finally {
      setValidating(false);
    }
  }

  const { errors } = formState;
  const title = edit ? `Edit node · ${node.name}` : "Add server";
  const submit = handleSubmit((values) => {
    setConflict(null);
    save.mutate(values);
  });

  return (
    <Sheet open dirty={dirty} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent title={title} className="max-md:h-[96dvh] max-md:max-h-[96dvh]">
        <form onSubmit={submit} noValidate className="flex flex-col gap-3">
          <p className="text-xs text-t2">
            {mode === "clone" && node ? `Prefilled from ${node.name} except the tuning profile · nothing is saved until you add it.` : null}
            {mode === "add" ? "Fields marked * are required. The tuning profile is set later, in Edit." : null}
            {edit && dirty ? <span className="font-semibold text-warn">● unsaved changes</span> : null}
          </p>
          {conflict === "active" && edit ? <ActiveBanner node={node} /> : null}
          {conflict === "identity" ? <p role="alert" className="rounded-xl border border-bad/40 bg-bad/10 p-3 text-sm text-t1">{IDENTITY_MESSAGE}</p> : null}

          <Section title="Identity">
            <TextField label="Name" required hint={`≤ ${MAX_FIELD}`} error={errors.name?.message} autoComplete="off" {...register("name")} />
            <TextField label="Note" hint="optional" error={errors.note?.message} autoComplete="off" {...register("note")} />
          </Section>

          <Section title="Endpoint">
            <div className="grid grid-cols-[minmax(0,1fr)_7rem] gap-2.5">
              <TextField label="Address" required error={errors.address?.message} autoComplete="off" className="font-mono" {...register("address")} />
              <TextField label="Port" required hint="1–65535" type="number" inputMode="numeric" error={errors.port?.message} {...register("port")} />
            </div>
            <TextField label="UUID" required error={errors.uuid?.message} autoComplete="off" className="font-mono" {...register("uuid")} />
          </Section>

          <Section title="Transport">
            <SegmentedField legend="Transport" options={["vision", "xhttp"]} radio={register("transport")} />
            {transport === "xhttp" ? (
              <div className="grid grid-cols-1 gap-2.5 md:grid-cols-3">
                <TextField label="Path" error={errors.path?.message} className="font-mono" {...register("path")} />
                <TextField label="Host" error={errors.host?.message} className="font-mono" {...register("host")} />
                <TextField label="Mode" error={errors.mode?.message} {...register("mode")} />
              </div>
            ) : null}
          </Section>

          <Section title="Security">
            <SegmentedField legend="Security" options={["reality", "tls"]} radio={register("security")} />
            {security === "reality" ? (
              <div className="grid grid-cols-1 gap-2.5 md:grid-cols-[minmax(0,1fr)_9rem]">
                <TextField label="Public key" error={errors.public_key?.message} className="font-mono" {...register("public_key")} />
                <TextField label="Short ID" error={errors.short_id?.message} className="font-mono" {...register("short_id")} />
              </div>
            ) : (
              <TextField label="ALPN" hint="tls only" placeholder="h2,http/1.1" error={errors.alpn?.message} className="font-mono" {...register("alpn")} />
            )}
            <TextField label="SNI" error={errors.sni?.message} className="font-mono" {...register("sni")} />
            {edit ? <TextField label="Fingerprint" error={errors.fingerprint?.message} {...register("fingerprint")} /> : null}
          </Section>

          {edit ? (
            <FieldShell id="node-form-profile" label="Tuning profile">
              {/* Controlled: the profiles arrive after the form opens, and the node's own must still show once they do. */}
              <Controller
                control={control}
                name="tuning_profile_id"
                render={({ field }) => <ProfileSelect id="node-form-profile" profiles={profiles.data} {...field} />}
              />
            </FieldShell>
          ) : null}

          <div className="sticky bottom-0 -mx-5 -mb-5 flex flex-wrap items-center gap-2 border-t border-line bg-solid px-5 py-3">
            <Button disabled={validating} onClick={() => void validate()}>{validating ? "Validating…" : "Validate"}</Button>
            {validation ? (
              <p role="status" className={cn("min-w-0 flex-1 truncate font-mono text-xs", validation.ok ? "text-ok" : "text-bad")} title={validation.text}>{validation.text}</p>
            ) : <span className="flex-1" />}
            <Button onClick={() => void closeGuarded(dirty, onClose)}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={save.isPending}>{save.isPending ? "Saving…" : edit ? "Save" : "Add server"}</Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
