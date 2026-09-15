// The add / edit / clone node form: its values, the limits the backend enforces (schemas.py NodeIn / NodeUpdate),
// what is sent for them, and how a failed write is explained. Pure and unit-tested.
import { z } from "zod";
import { ApiError, errText, type Node, type NodeIn, type NodeUpdate, type NodeValidateIn } from "../../api/client";

/** backend _MAX_FIELD / _MAX_HOST */
export const MAX_FIELD = 512;
export const MAX_HOST = 253;

/** N13: any 409 on edit or delete means the node is active (Svelte's mapping). */
export const ACTIVE_NODE_MESSAGE = "That node is active. Disconnect → Edit → Connect, then try again.";
/** N12: the 409 of Add server. */
export const IDENTITY_MESSAGE = "a node with this identity already exists";
export const PORT_MESSAGE = "port must be 1–65535";

export type Transport = "vision" | "xhttp";
export type Security = "reality" | "tls";

/** Every field is a string, as the inputs hold them; `tuning_profile_id` is the select's value ("" = the default). */
export interface NodeFormValues {
  name: string;
  address: string;
  port: string;
  uuid: string;
  transport: Transport;
  security: Security;
  sni: string;
  public_key: string;
  short_id: string;
  alpn: string;
  path: string;
  host: string;
  mode: string;
  note: string;
  fingerprint: string;
  tuning_profile_id: string;
}

const required = (label: string, max: number) =>
  z.string().trim().min(1, `${label} is required`).max(max, `${label} is at most ${max} characters`);
const optional = (label: string, max: number) => z.string().max(max, `${label} is at most ${max} characters`);

/**
 * N12 / N13: the backend's limits. Add and edit share them — Add shows no fingerprint or tuning profile, and sends
 * the fingerprint it was opened with ("chrome", or the cloned node's).
 */
export const nodeFormSchema = z.object({
  name: required("name", MAX_FIELD),
  address: required("address", MAX_HOST),
  port: z.string().trim().regex(/^\d{1,5}$/, PORT_MESSAGE).refine((port) => Number(port) >= 1 && Number(port) <= 65_535, PORT_MESSAGE),
  uuid: required("uuid", MAX_FIELD),
  transport: z.enum(["vision", "xhttp"]),
  security: z.enum(["reality", "tls"]),
  sni: optional("SNI", MAX_HOST),
  public_key: optional("public key", MAX_FIELD),
  short_id: optional("short id", MAX_FIELD),
  alpn: optional("ALPN", MAX_FIELD),
  path: optional("path", MAX_FIELD),
  host: optional("host", MAX_HOST),
  mode: optional("mode", 64),
  note: optional("note", MAX_FIELD),
  fingerprint: optional("fingerprint", 32),
  tuning_profile_id: z.string(),
}) satisfies z.ZodType<NodeFormValues, NodeFormValues>;

export const BLANK_NODE_FORM: NodeFormValues = {
  name: "", address: "", port: "443", uuid: "", transport: "vision", security: "reality",
  sni: "", public_key: "", short_id: "", alpn: "", path: "", host: "", mode: "", note: "",
  fingerprint: "chrome", tuning_profile_id: "",
};

/** The select value of a profile id, and back: "" is "(default)". */
export function profileValue(id: number | null): string {
  return id === null ? "" : String(id);
}

export function profileFromValue(value: string): number | null {
  return value === "" ? null : Number(value);
}

/** The form values of a stored node. */
export function nodeToForm(node: Node): NodeFormValues {
  return {
    name: node.name, address: node.address, port: String(node.port), uuid: node.uuid,
    transport: node.transport === "xhttp" ? "xhttp" : "vision",
    security: node.security === "tls" ? "tls" : "reality",
    sni: node.sni, public_key: node.public_key, short_id: node.short_id, alpn: node.alpn,
    path: node.path, host: node.host, mode: node.mode, note: node.note,
    fingerprint: node.fingerprint, tuning_profile_id: profileValue(node.tuning_profile_id),
  };
}

/** N14: Add prefilled from a node — its name + " copy" and every field except the tuning profile. */
export function cloneToForm(node: Node): NodeFormValues {
  return { ...nodeToForm(node), name: `${node.name} copy`, tuning_profile_id: "" };
}

/** The form's fields, trimmed, with no hiding applied — shared by the add and edit mappings below. */
function trimmedFields(values: NodeFormValues): NodeIn {
  return {
    name: values.name.trim(),
    address: values.address.trim(),
    port: Number(values.port),
    uuid: values.uuid.trim(),
    transport: values.transport,
    security: values.security,
    sni: values.sni.trim(),
    public_key: values.public_key.trim(),
    short_id: values.short_id.trim(),
    alpn: values.alpn.trim(),
    path: values.path.trim(),
    host: values.host.trim(),
    mode: values.mode.trim(),
    note: values.note,
    fingerprint: values.fingerprint.trim(),
  };
}

/**
 * What Add sends. Fields the chosen transport or security hides go out empty, so what was typed under a choice
 * that is no longer selected is never saved: reality keeps public key + short id, tls keeps ALPN, xhttp keeps
 * path / host / mode. A new node has nothing stored to protect.
 */
export function formToNodeIn(values: NodeFormValues): NodeIn {
  const reality = values.security === "reality";
  const xhttp = values.transport === "xhttp";
  const fields = trimmedFields(values);
  return {
    ...fields,
    public_key: reality ? fields.public_key : "",
    short_id: reality ? fields.short_id : "",
    alpn: reality ? "" : fields.alpn,
    path: xhttp ? fields.path : "",
    host: xhttp ? fields.host : "",
    mode: xhttp ? fields.mode : "",
  };
}

/**
 * N13: the edit patch — every field as-is (trimmed), hidden ones included, plus the tuning profile. Unlike Add,
 * edit must not erase a value already stored under a transport or security that is no longer selected.
 */
export function formToNodeUpdate(values: NodeFormValues): NodeUpdate {
  return { ...trimmedFields(values), tuning_profile_id: profileFromValue(values.tuning_profile_id) };
}

/**
 * N12 / N13: what Validate sends. On edit it mirrors the edit payload exactly (unzeroed, with the selected
 * profile), so Validate checks exactly what Save will send; on add it mirrors Add (zeroed, no profile).
 */
export function formToValidate(values: NodeFormValues, withProfile: boolean): NodeValidateIn {
  return withProfile
    ? { ...trimmedFields(values), tuning_profile_id: profileFromValue(values.tuning_profile_id) }
    : formToNodeIn(values);
}

/**
 * The backend's other 409 on an edit (`PATCH /nodes/{id}`): the edited fields collide with another node's identity.
 * Told apart from the active-node 409 by the server's detail, so the form never offers a Disconnect that cannot help.
 */
export function isIdentityConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.detail === IDENTITY_MESSAGE;
}

/** N13 / N15: an edit or delete that failed — a 409 is the active node, unless the server reports an identity clash. */
export function nodeMutationMessage(error: unknown, fallback: string): string {
  if (isIdentityConflict(error)) return IDENTITY_MESSAGE;
  return error instanceof ApiError && error.status === 409 ? ACTIVE_NODE_MESSAGE : errText(error, fallback);
}

/** N12: an add that failed — a 409 is a node with the same identity. */
export function addNodeMessage(error: unknown): string {
  return error instanceof ApiError && error.status === 409 ? IDENTITY_MESSAGE : errText(error, "add failed");
}

/** Validate's inline result. */
export function validateMessage(result: { ok: boolean; error: string }): { ok: boolean; text: string } {
  return result.ok ? { ok: true, text: "✓ config valid" } : { ok: false, text: `✗ ${result.error}` };
}
