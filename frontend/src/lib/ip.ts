// IP address parsing as the backend's Python `ipaddress` reads it, for form checks that must agree with the gateway.

const OCTET = /^(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;

/** A dotted IPv4 address as a number, as Python's ipaddress reads one (no leading zeros); null otherwise. */
export function ipv4Number(text: string): number | null {
  const parts = text.split(".");
  if (parts.length !== 4 || !parts.every((part) => OCTET.test(part))) return null;
  return parts.reduce((total, part) => total * 256 + Number(part), 0);
}

function prefixOf(text: string | undefined, max: number): number | null {
  if (text === undefined) return max;
  if (!/^\d{1,3}$/.test(text)) return null;
  const prefix = Number(text);
  return prefix <= max ? prefix : null;
}

/** An IPv4 address or CIDR as its first and last address; null when it is neither. Host bits are allowed (strict=False). */
export function ipv4Span(text: string): { first: number; last: number } | null {
  const [address, prefixText, ...rest] = text.split("/");
  if (rest.length > 0) return null;
  const base = ipv4Number(address!);
  const prefix = prefixOf(prefixText, 32);
  if (base === null || prefix === null) return null;
  const size = 2 ** (32 - prefix);
  const first = Math.floor(base / size) * size;
  return { first, last: first + size - 1 };
}

const HEX_GROUP = /^[0-9a-f]{1,4}$/i;

/** An IPv6 address: eight hex groups, one `::` at most, an optional dotted IPv4 tail. */
export function isIPv6Address(text: string): boolean {
  let groups = text;
  let extra = 0;
  const lastColon = groups.lastIndexOf(":");
  if (groups.includes(".")) {
    if (ipv4Number(groups.slice(lastColon + 1)) === null) return false;
    groups = `${groups.slice(0, lastColon + 1)}0`;
    extra = 1;
  }
  const halves = groups.split("::");
  if (halves.length > 2) return false;
  const parts = halves.map((half) => (half === "" ? [] : half.split(":")));
  if (!parts.every((list) => list.every((group) => HEX_GROUP.test(group)))) return false;
  const count = parts.flat().length + extra;
  return halves.length === 2 ? count <= 7 : count === 8;
}

/** The prefix length of an IPv6 address or CIDR (128 without one); null when it is neither. */
export function ipv6PrefixLength(text: string): number | null {
  const [address, prefixText, ...rest] = text.split("/");
  if (rest.length > 0 || !address!.includes(":") || !isIPv6Address(address!)) return null;
  return prefixOf(prefixText, 128);
}

/** An IPv6 address or CIDR (prefix 0–128). */
export function isIPv6Network(text: string): boolean {
  return ipv6PrefixLength(text) !== null;
}
