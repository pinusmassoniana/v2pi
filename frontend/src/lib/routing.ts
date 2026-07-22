function ipv4Int(value: string): number | null {
  const parts = value.split(".");
  if (parts.length !== 4) return null;
  let result = 0;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const octet = Number(part);
    if (octet > 255) return null;
    result = result * 256 + octet;
  }
  return result >>> 0;
}

export function inIPv4Cidr(ip: string, cidr: string): boolean {
  const pieces = cidr.split("/");
  if (pieces.length > 2) return false;
  const address = ipv4Int(ip);
  const base = ipv4Int(pieces[0]);
  const prefix = pieces.length === 1 ? 32 : Number(pieces[1]);
  if (address === null || base === null || !Number.isInteger(prefix) || prefix < 0 || prefix > 32) return false;
  if (prefix === 0) return true;
  const mask = (0xffffffff << (32 - prefix)) >>> 0;
  return (address & mask) === (base & mask);
}

export function parseDestination(raw: string): { host: string; port: number | null; ipv6: boolean } {
  const bracketed = raw.match(/^\[([^\]]+)](?::(\d+))?$/);
  if (bracketed) return { host: bracketed[1], port: bracketed[2] ? Number(bracketed[2]) : null, ipv6: true };
  if ((raw.match(/:/g) ?? []).length > 1) return { host: raw, port: null, ipv6: true };
  const withPort = raw.match(/^(.*):(\d+)$/);
  return { host: withPort?.[1] ?? raw, port: withPort ? Number(withPort[2]) : null, ipv6: false };
}
