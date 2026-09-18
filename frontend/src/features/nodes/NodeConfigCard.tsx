import type { Node } from "../../api/client";
import { CardHeader } from "../../components/data/CardHeader";
import { KeyValueRows, type KeyValueRow } from "../../components/data/KeyValueRows";
import { GlassCard } from "../../components/ui/GlassCard";

const mono = (value: string) => (value ? <span className="font-mono">{value}</span> : "—");

/** N5 config for one node: always address, port, transport, security, SNI, fingerprint, flow, note; the rest by kind. */
export function NodeConfigCard({ node }: { node: Node }) {
  const vless = node.protocol !== "trojan" && node.protocol !== "shadowsocks";
  const rows: KeyValueRow[] = [
    { key: "Protocol", value: node.protocol || "vless" },
    { key: "Address", value: mono(node.address) },
    { key: "Port", value: mono(String(node.port)) },
  ];
  // B4: a node that is not VLESS carries none of the VLESS shape, and shadowsocks has no TLS
  // layer of its own — showing empty Transport/SNI rows for it would be inventing fields.
  if (node.protocol === "shadowsocks") {
    rows.push({ key: "Cipher", value: mono(node.method) }, { key: "Password", value: node.has_password ? "stored" : "—" });
  } else {
    if (vless) rows.push({ key: "Transport", value: node.transport });
    if (!vless) rows.push({ key: "Password", value: node.has_password ? "stored" : "—" });
    rows.push({ key: "Security", value: node.security }, { key: "SNI", value: mono(node.sni) });
    if (node.security === "reality") rows.push({ key: "Public key", value: mono(node.public_key) }, { key: "Short ID", value: mono(node.short_id) });
    if (node.security === "tls") rows.push({ key: "ALPN", value: mono(node.alpn) });
    if (vless && node.transport === "xhttp") rows.push({ key: "Path", value: mono(node.path) }, { key: "Host", value: mono(node.host) }, { key: "Mode", value: node.mode || "—" });
    rows.push({ key: "Fingerprint", value: node.fingerprint || "—" });
    if (vless) rows.push({ key: "Flow", value: node.transport === "vision" ? mono("xtls-rprx-vision") : "—" });
  }
  rows.push({ key: "Note", value: node.note || "—" });
  return (
    <GlassCard aria-label="Config">
      <CardHeader title="Config" level={3} />
      <KeyValueRows rows={rows} />
    </GlassCard>
  );
}
