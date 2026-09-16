import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { NODES, PROFILES, SERVER_NODES, node } from "../../test/fixtures";
import {
  ACTIVE_NODE_MESSAGE, BLANK_NODE_FORM, IDENTITY_MESSAGE, addNodeMessage, cloneToForm, formToNodeIn, formToNodeUpdate, formToValidate,
  isIdentityConflict, nodeFormSchema, nodeMutationMessage, nodeToForm, validateMessage, type NodeFormValues,
} from "./nodeForm";
import { GLOBAL_DEFAULT, profileFromValue, profileName, profileValue } from "../../lib/profiles";

const VALID: NodeFormValues = { ...BLANK_NODE_FORM, name: "vps-ams-02", address: "198.51.100.77", uuid: "3f1c9a52" };
const issues = (values: NodeFormValues) => {
  const result = nodeFormSchema.safeParse(values);
  return result.success ? {} : Object.fromEntries(result.error.issues.map((issue) => [issue.path.join("."), issue.message]));
};

describe("node form schema", () => {
  it("accepts a blank form once name, address and uuid are filled", () => {
    expect(nodeFormSchema.safeParse(BLANK_NODE_FORM).success).toBe(false);
    expect(issues(BLANK_NODE_FORM)).toEqual({ name: "name is required", address: "address is required", uuid: "uuid is required" });
    expect(nodeFormSchema.safeParse(VALID).success).toBe(true);
  });

  it("mirrors the backend's limits", () => {
    expect(issues({ ...VALID, name: "n".repeat(513) })).toEqual({ name: "name is at most 512 characters" });
    expect(issues({ ...VALID, name: "n".repeat(512) })).toEqual({});
    expect(issues({ ...VALID, address: "a".repeat(254) })).toEqual({ address: "address is at most 253 characters" });
    expect(issues({ ...VALID, uuid: "u".repeat(513) })).toEqual({ uuid: "uuid is at most 512 characters" });
    expect(issues({ ...VALID, sni: "s".repeat(254), mode: "m".repeat(65), fingerprint: "f".repeat(33) })).toEqual({
      sni: "SNI is at most 253 characters", mode: "mode is at most 64 characters", fingerprint: "fingerprint is at most 32 characters",
    });
    expect(issues({ ...VALID, name: "   " })).toEqual({ name: "name is required" });
  });

  it("port is a whole number from 1 to 65535", () => {
    for (const port of ["1", "443", "65535", " 8443 "]) expect(issues({ ...VALID, port })).toEqual({});
    for (const port of ["0", "65536", "70000", "", "44.3", "-1", "port"]) expect(issues({ ...VALID, port })).toEqual({ port: "port must be 1–65535" });
  });
});

describe("what the form sends", () => {
  it("vision · reality keeps the reality keys and drops ALPN and the xhttp fields", () => {
    const values: NodeFormValues = { ...VALID, port: " 8443", sni: " www.microsoft.com ", public_key: "pbk", short_id: "sid", alpn: "h2", path: "/xh", host: "h", mode: "auto", note: " own VPS " };
    expect(formToNodeIn(values)).toEqual({
      name: "vps-ams-02", address: "198.51.100.77", port: 8443, uuid: "3f1c9a52", transport: "vision", security: "reality",
      sni: "www.microsoft.com", public_key: "pbk", short_id: "sid", alpn: "", path: "", host: "", mode: "", note: " own VPS ", fingerprint: "chrome",
    });
  });

  it("xhttp · tls keeps path, host, mode and ALPN and drops the reality keys", () => {
    const values: NodeFormValues = { ...VALID, transport: "xhttp", security: "tls", public_key: "pbk", short_id: "sid", alpn: "h2,http/1.1", path: "/xh-7c1", host: "cdn.example.net", mode: "auto" };
    expect(formToNodeIn(values)).toMatchObject({ transport: "xhttp", security: "tls", public_key: "", short_id: "", alpn: "h2,http/1.1", path: "/xh-7c1", host: "cdn.example.net", mode: "auto" });
  });

  it("edit adds the tuning profile; Validate sends it only for an edit", () => {
    expect(formToNodeUpdate({ ...VALID, tuning_profile_id: "2" })).toMatchObject({ name: "vps-ams-02", tuning_profile_id: 2 });
    expect(formToNodeUpdate({ ...VALID, tuning_profile_id: "" })).toMatchObject({ tuning_profile_id: null });
    expect(formToValidate({ ...VALID, tuning_profile_id: "2" }, true)).toMatchObject({ tuning_profile_id: 2 });
    expect(formToValidate({ ...VALID, tuning_profile_id: "2" }, false)).not.toHaveProperty("tuning_profile_id");
    expect(profileValue(null)).toBe("");
    expect(profileValue(2)).toBe("2");
    expect(profileFromValue("")).toBeNull();
    expect(profileFromValue("2")).toBe(2);
    expect(GLOBAL_DEFAULT).toBe("(global default)");
    expect(profileName(PROFILES, null)).toBe("(global default)");
    expect(profileName(PROFILES, 2)).toBe("fragment-tls");
    expect(profileName(undefined, 2)).toBe("profile #2");
  });

  it("edit keeps the stored values of fields the current transport or security hides; add and validate-on-add still drop them", () => {
    const reality = nodeToForm({ ...node(90, "reality-node"), security: "reality", alpn: "h2" });
    expect(formToNodeIn(reality)).toMatchObject({ alpn: "" });
    expect(formToNodeUpdate(reality)).toMatchObject({ alpn: "h2" });
    expect(formToValidate(reality, true)).toMatchObject({ alpn: "h2" });
    expect(formToValidate(reality, false)).toMatchObject({ alpn: "" });

    const vision = nodeToForm({ ...node(91, "vision-node"), transport: "vision", path: "/stale", host: "old.example", mode: "auto" });
    expect(formToNodeIn(vision)).toMatchObject({ path: "", host: "", mode: "" });
    expect(formToNodeUpdate(vision)).toMatchObject({ path: "/stale", host: "old.example", mode: "auto" });
    expect(formToValidate(vision, true)).toMatchObject({ path: "/stale", host: "old.example", mode: "auto" });
    expect(formToValidate(vision, false)).toMatchObject({ path: "", host: "", mode: "" });
  });
});

describe("prefilled forms", () => {
  it("edit starts from the stored node, its profile included", () => {
    expect(nodeToForm(NODES[0]!)).toEqual({
      name: "nl-ams-03", address: "nl-ams-03.example.org", port: "443", uuid: "uuid-1", transport: "vision", security: "reality",
      sni: "www.microsoft.com", public_key: "Zm9vX3JlYWxpdHlfcHViX2tleQ", short_id: "6ba85179e3", alpn: "", path: "", host: "", mode: "", note: "",
      fingerprint: "chrome", tuning_profile_id: "2",
    });
    expect(nodeToForm(NODES[3]!)).toMatchObject({ port: "8443", transport: "xhttp", security: "tls", alpn: "h2,http/1.1" });
  });

  it("an unknown stored transport or security opens as the defaults", () => {
    expect(nodeToForm({ ...SERVER_NODES[0]!, transport: "grpc", security: "none" })).toMatchObject({ transport: "vision", security: "reality" });
  });

  it("clone: name + copy and every field except the tuning profile", () => {
    expect(cloneToForm(NODES[0]!)).toEqual({ ...nodeToForm(NODES[0]!), name: "nl-ams-03 copy", tuning_profile_id: "" });
  });
});

describe("messages", () => {
  it("a 409 on edit or delete explains the active node, unless the server says it is an identity clash; other errors carry the backend's text", () => {
    expect(nodeMutationMessage(new ApiError(409, "disconnect the active node before editing it"), "save failed")).toBe(ACTIVE_NODE_MESSAGE);
    expect(nodeMutationMessage(new ApiError(409, "node 3 is active"), "delete failed")).toBe(ACTIVE_NODE_MESSAGE);
    expect(nodeMutationMessage(new ApiError(409, "a node with this identity already exists"), "save failed")).toBe(IDENTITY_MESSAGE);
    expect(nodeMutationMessage(new ApiError(422, "tuning profile not found"), "save failed")).toBe("tuning profile not found");
    expect(nodeMutationMessage(new Error("boom"), "save failed")).toBe("save failed");
    expect(ACTIVE_NODE_MESSAGE).toBe("That node is active. Disconnect → Edit → Connect, then try again.");
  });

  it("an identity clash is the backend's 409 with the identity detail, and nothing else", () => {
    expect(isIdentityConflict(new ApiError(409, "a node with this identity already exists"))).toBe(true);
    expect(isIdentityConflict(new ApiError(409, "disconnect the active node before editing it"))).toBe(false);
    expect(isIdentityConflict(new ApiError(422, "a node with this identity already exists"))).toBe(false);
    expect(isIdentityConflict(new Error("a node with this identity already exists"))).toBe(false);
    expect(isIdentityConflict(null)).toBe(false);
  });

  it("a 409 on add is a node with the same identity", () => {
    expect(addNodeMessage(new ApiError(409, "whatever"))).toBe(IDENTITY_MESSAGE);
    expect(addNodeMessage(new ApiError(422, "port: out of range"))).toBe("port: out of range");
    expect(addNodeMessage(null)).toBe("add failed");
  });

  it("validate reads ✓ config valid or ✗ with the error", () => {
    expect(validateMessage({ ok: true, error: "" })).toEqual({ ok: true, text: "✓ config valid" });
    expect(validateMessage({ ok: false, error: "reality: bad public key" })).toEqual({ ok: false, text: "✗ reality: bad public key" });
  });
});
