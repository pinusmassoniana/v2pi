import { describe, expect, it } from "vitest";
import { NODES, SERVER_NODES, node } from "../../test/fixtures";
import { nodeJson, vlessUri } from "./vless";

describe("vless share link", () => {
  it("vision · reality: flow, public key and short id, no xhttp or alpn fields", () => {
    expect(vlessUri(NODES[0]!)).toBe(
      "vless://uuid-1@nl-ams-03.example.org:443?type=tcp&security=reality&sni=www.microsoft.com&pbk=Zm9vX3JlYWxpdHlfcHViX2tleQ&sid=6ba85179e3&fp=chrome&flow=xtls-rprx-vision#nl-ams-03",
    );
  });

  it("xhttp · tls: path, host and mode only for xhttp, alpn, no flow, no reality keys", () => {
    const xhttp = { ...node(30, "edge"), transport: "xhttp", network: "xhttp", security: "tls", public_key: "ignored", short_id: "ignored", path: "/xh-7c1", host: "cdn.example.net", mode: "auto", alpn: "h2,http/1.1" };
    expect(vlessUri(xhttp)).toBe(
      "vless://uuid-30@edge.example.org:443?type=xhttp&security=tls&fp=chrome&path=%2Fxh-7c1&host=cdn.example.net&mode=auto&alpn=h2%2Chttp%2F1.1#edge",
    );
    expect(vlessUri(NODES[3]!)).toBe("vless://uuid-4@pl-waw-01.example.org:8443?type=xhttp&security=tls&fp=chrome&alpn=h2%2Chttp%2F1.1#pl-waw-01");
  });

  it("a vision node never carries xhttp fields, even when some are stored", () => {
    expect(vlessUri({ ...SERVER_NODES[0]!, path: "/stale", host: "old.example", mode: "auto" })).toBe(
      "vless://uuid-7@198.51.100.23:443?type=tcp&security=reality&pbk=dnBzLWhlbC1wdWI&fp=chrome&flow=xtls-rprx-vision#vps-hel",
    );
  });

  it("brackets an IPv6 host once and encodes the name", () => {
    const v6 = { ...node(31, "x"), address: "2001:db8::1", name: "Frankfurt #2 · backup", fingerprint: "" };
    expect(vlessUri(v6)).toBe("vless://uuid-31@[2001:db8::1]:443?type=tcp&security=reality&flow=xtls-rprx-vision#Frankfurt%20%232%20%C2%B7%20backup");
    expect(vlessUri({ ...v6, address: "[2001:db8::1]" })).toContain("@[2001:db8::1]:443?");
  });

  it("the JSON is the full node, pretty-printed", () => {
    expect(JSON.parse(nodeJson(NODES[0]!))).toEqual(NODES[0]);
    expect(nodeJson(NODES[0]!)).toContain('\n  "name": "nl-ams-03",\n');
  });
});
