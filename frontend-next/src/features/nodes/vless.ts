import type { Node } from "../../api/client";
import { formatUriHost } from "../../lib/format";

export type ShareableNode = Pick<
  Node,
  "name" | "address" | "port" | "uuid" | "transport" | "network" | "security" | "sni" | "public_key" | "short_id" | "fingerprint" | "path" | "host" | "mode" | "alpn"
>;

/**
 * N16: the node as a vless:// share link, exactly as the Svelte panel built it —
 * `vless://uuid@host:port?type&security&sni&pbk&sid&fp&flow&path&host&mode&alpn#name`. `flow` only for vision,
 * `pbk`/`sid` only for reality, `path`/`host`/`mode` only for xhttp, empty values left out, IPv6 hosts bracketed.
 */
export function vlessUri(node: ShareableNode): string {
  const xhttp = node.network === "xhttp";
  const params = new URLSearchParams();
  params.set("type", xhttp ? "xhttp" : "tcp");
  params.set("security", node.security);
  if (node.sni) params.set("sni", node.sni);
  if (node.security === "reality") {
    if (node.public_key) params.set("pbk", node.public_key);
    if (node.short_id) params.set("sid", node.short_id);
  }
  if (node.fingerprint) params.set("fp", node.fingerprint);
  if (node.transport === "vision") params.set("flow", "xtls-rprx-vision");
  if (xhttp) {
    if (node.path) params.set("path", node.path);
    if (node.host) params.set("host", node.host);
    if (node.mode) params.set("mode", node.mode);
  }
  if (node.alpn) params.set("alpn", node.alpn);
  return `vless://${node.uuid}@${formatUriHost(node.address)}:${node.port}?${params.toString()}#${encodeURIComponent(node.name)}`;
}

/** N16: the full node as JSON, as the Export dialog shows and copies it. */
export function nodeJson(node: Node): string {
  return JSON.stringify(node, null, 2);
}
