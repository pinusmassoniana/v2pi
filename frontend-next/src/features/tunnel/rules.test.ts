import { describe, expect, it } from "vitest";
import { RU_DIRECT_PRESET, TUNNEL_ROUTING } from "../../test/fixtures";
import {
  GEO_TOKENS, PRIVATE_IPV4_RANGES, VALUE_PLACEHOLDERS, addRule, applyPreset, changeCount, changesLabel, exportJson, importJson, isIPv6Network,
  isPrivateIPv4, isStaged, moveRule, removeRule, resetRouting, stagedFromRouting, testDestination, toRoutingIn, updateRule, validateRuleRow,
  type StagedRouting,
} from "./rules";

const saved = () => stagedFromRouting(TUNNEL_ROUTING);
const keyOf = (state: StagedRouting, value: string) => state.rows.find((row) => row.value === value)!.key;

describe("staged state", () => {
  it("builds rows from the gateway's ruleset, keyed by id, so the same data builds the same rows", () => {
    const state = saved();
    expect(state.defaultAction).toBe("proxy");
    expect(state.domainStrategy).toBe("IPIfNonMatch");
    expect(state.rows[1]).toEqual({ key: "s22", id: 22, type: "geoip", value: "ru", action: "direct", enabled: true, label: "RU off tunnel" });
    expect(saved()).toEqual(state);
    expect(isStaged(state, saved())).toBe(false);
  });

  it("a preset reply replaces the rows; its unsaved rules (id 0) get fresh keys and no id", () => {
    const staged = applyPreset(RU_DIRECT_PRESET);
    const added = staged.rows.at(-1)!;
    expect(added).toMatchObject({ id: null, type: "geosite", value: "category-ru", action: "direct" });
    expect(added.key).toMatch(/^n\d+$/);
    expect(applyPreset(RU_DIRECT_PRESET).rows.at(-1)!.key).not.toBe(added.key);
  });

  it("is staged by any difference in rows, their order, the default action or the domain strategy", () => {
    const base = saved();
    expect(isStaged(base, updateRule(base, "s22", { label: "RU" }))).toBe(true);
    expect(isStaged(base, moveRule(base, "s22", 1))).toBe(true);
    expect(isStaged(base, addRule(base, "n-test"))).toBe(true);
    expect(isStaged(base, { ...base, defaultAction: "direct" })).toBe(true);
    expect(isStaged(base, { ...base, domainStrategy: "AsIs" })).toBe(true);
    expect(isStaged(base, updateRule(updateRule(base, "s22", { label: "x" }), "s22", { label: "RU off tunnel" }))).toBe(false);
  });

  it("add appends a proxied domain rule with no value; update, remove and move act on one row by key", () => {
    const base = saved();
    const added = addRule(base, "n-new");
    expect(added.rows.at(-1)).toEqual({ key: "n-new", id: null, type: "domain", value: "", action: "proxy", enabled: true, label: "" });
    expect(removeRule(base, "s21").rows.map((row) => row.id)).toEqual([22, 23, 24, 25, 26]);
    expect(moveRule(base, "s21", 1).rows.map((row) => row.id)).toEqual([22, 21, 23, 24, 25, 26]);
    expect(moveRule(base, "s21", -1)).toBe(base);
    expect(moveRule(base, "s26", 1)).toBe(base);
    const updated = updateRule(base, "s25", { value: "25, 465" });
    expect(updated.rows[4]!.value).toBe("25, 465");
    expect(updated.rows[0]).toBe(base.rows[0]);
  });

  it("reset stages no rules, default proxy and IPIfNonMatch", () => {
    expect(resetRouting()).toEqual({ rows: [], defaultAction: "proxy", domainStrategy: "IPIfNonMatch" });
  });
});

describe("changeCount (the STAGED banner)", () => {
  it("nothing changed is zero", () => {
    expect(changeCount(saved(), saved())).toBe(0);
  });

  it("an added rule counts once it has a value; an empty one never does", () => {
    const base = saved();
    const added = addRule(base, "n-a");
    expect(changeCount(base, added)).toBe(0);
    expect(changeCount(base, updateRule(added, "n-a", { value: "   " }))).toBe(0);
    expect(changeCount(base, updateRule(added, "n-a", { value: "example.com" }))).toBe(1);
    expect(changesLabel(1)).toBe("1 change");
    expect(changesLabel(3)).toBe("3 changes");
  });

  it("a removed saved rule counts, and so does a saved rule emptied (Save drops it)", () => {
    const base = saved();
    expect(changeCount(base, removeRule(base, "s23"))).toBe(1);
    expect(changeCount(base, updateRule(base, "s23", { value: "" }))).toBe(1);
  });

  it("each saved rule counts once for any change of type, value, action, on/off or label", () => {
    const base = saved();
    expect(changeCount(base, updateRule(base, "s24", { type: "domain" }))).toBe(1);
    expect(changeCount(base, updateRule(base, "s24", { value: "45.84.0.0/16" }))).toBe(1);
    expect(changeCount(base, updateRule(base, "s24", { action: "block" }))).toBe(1);
    expect(changeCount(base, updateRule(base, "s26", { enabled: true }))).toBe(1);
    expect(changeCount(base, updateRule(base, "s24", { label: "hq" }))).toBe(1);
    expect(changeCount(base, updateRule(base, "s24", { value: "45.84.0.0/16", action: "block", label: "hq" }))).toBe(1);
    expect(changeCount(base, updateRule(base, "s24", { value: " 45.83.0.0/16 " }))).toBe(0);
  });

  it("a single move counts the two rules it moved, once each, even if one of them was edited too", () => {
    const base = saved();
    const moved = moveRule(base, "s22", 1);
    expect(changeCount(base, moved)).toBe(2);
    expect(changeCount(base, updateRule(moved, "s22", { label: "RU" }))).toBe(2);
    // a rule carried from the top to the bottom shifts every other rule up by one
    let carried = base;
    for (let step = 0; step < 5; step++) carried = moveRule(carried, "s21", 1);
    expect(changeCount(base, carried)).toBe(6);
  });

  it("removing a rule does not make the rules below it count as moved", () => {
    const base = saved();
    expect(changeCount(base, removeRule(base, "s21"))).toBe(1);
    expect(changeCount(base, addRule(removeRule(base, "s21"), "n-b"))).toBe(1);
  });

  it("the default action and the domain strategy count one each", () => {
    const base = saved();
    expect(changeCount(base, { ...base, defaultAction: "block" })).toBe(1);
    expect(changeCount(base, { ...base, defaultAction: "block", domainStrategy: "IPOnDemand" })).toBe(2);
    expect(changeCount(base, resetRouting())).toBe(6 + 0);
  });

  it("a preset reply counts only its new rules", () => {
    expect(changeCount(saved(), applyPreset(RU_DIRECT_PRESET))).toBe(1);
  });
});

describe("toRoutingIn (what Save and Validate send)", () => {
  it("trims values, drops empty rows, keeps enabled and label", () => {
    const base = saved();
    const state = updateRule(addRule(updateRule(base, "s22", { value: "  ru  " }), "n-e"), "n-e", { value: "  " });
    const { body, dropped } = toRoutingIn(state);
    expect(dropped).toBe(0);
    expect(body.default_action).toBe("proxy");
    expect(body.domain_strategy).toBe("IPIfNonMatch");
    expect(body.rules).toHaveLength(6);
    expect(body.rules[1]).toEqual({ type: "geoip", value: "ru", action: "direct", enabled: true, label: "RU off tunnel" });
    expect(body.rules[5]).toEqual({ type: "domain", value: "netflix.com", action: "proxy", enabled: false, label: "" });
  });

  it("drops rows repeating an earlier type, trimmed value and action, keeping the first, and counts them", () => {
    let state = addRule(saved(), "n-1");
    state = updateRule(state, "n-1", { type: "geoip", value: " ru", action: "direct", label: "copy" });
    state = updateRule(addRule(state, "n-2"), "n-2", { type: "geoip", value: "ru", action: "proxy" });
    const { body, dropped } = toRoutingIn(state);
    expect(dropped).toBe(1);
    expect(body.rules.filter((rule) => rule.value === "ru").map((rule) => [rule.action, rule.label])).toEqual([["direct", "RU off tunnel"], ["proxy", ""]]);
  });

  it("exports that body as 2-space JSON", () => {
    const text = exportJson(saved());
    expect(text).toContain('\n  "rules": [');
    expect(JSON.parse(text)).toEqual(toRoutingIn(saved()).body);
  });
});

describe("validateRuleRow (inline hints)", () => {
  const check = (type: string, value: string, action = "proxy") => validateRuleRow({ type: type as never, value, action: action as never });

  it("a value is required", () => {
    expect(check("domain", "")).toBe("value required");
    expect(check("geoip", " , ")).toBe("value required");
    expect(check("domain", "example.com")).toBeNull();
  });

  it("ports: N or A-B within 1–65535 with A ≤ B, and lists of them", () => {
    for (const ok of ["443", "1-65535", "1000-2000", "80,443", "80, 443\n8443", "5-5"]) expect(check("port", ok)).toBeNull();
    for (const bad of ["0", "65536", "2000-1000", "1-", "-5", "abc", "80;443", "123456", "1-65536"]) {
      expect(check("port", bad)).toMatch(/^bad port "/);
    }
    expect(check("port", "80,0")).toBe('bad port "0" — use 443, 1000-2000 or 80,443 within 1–65535');
  });

  it("ip: an IPv4 or IPv6 address or CIDR", () => {
    for (const ok of ["1.2.3.4", "45.83.0.0/16", "45.83.1.1/16", "0.0.0.0/0", "2001:db8::/32", "::1", "fe80::1/64", "::ffff:1.2.3.4", "1:2:3:4:5:6:7:8"]) {
      expect(check("ip", ok, "direct")).toBeNull();
    }
    for (const bad of ["1.2.3", "1.2.3.256", "01.2.3.4", "1.2.3.4/33", "1.2.3.4/a", "2001:db8::/129", "1:2:3:4:5:6:7:8:9", "1::2::3", "example.com", "1.2.3.4/8/8"]) {
      expect(check("ip", bad, "direct")).toBe(`bad ip/cidr "${bad}"`);
    }
    expect(isIPv6Network("1:2:3:4:5:6:7::")).toBe(true);
    expect(isIPv6Network("gggg::1")).toBe(false);
  });

  it("a private range sent anywhere but direct is shadowed by the built-in private → direct rule", () => {
    expect(check("ip", "8.8.8.8, 10.1.0.0/16", "proxy")).toBe('"10.1.0.0/16" is a private range — the built-in private → direct rule is matched first, so only direct works');
    expect(check("ip", "10.1.0.0/16", "direct")).toBeNull();
    expect(check("ip", "203.0.113.0/24", "block")).toMatch(/private range/);
    expect(check("ip", "100.64.0.0/10", "proxy")).toBeNull();
    expect(check("geoip", "ru, PRIVATE", "block")).toBe("geoip:private — the built-in private → direct rule is matched first, so only direct works");
    expect(check("geoip", "private", "direct")).toBeNull();
    expect(check("geosite", "private", "proxy")).toBeNull();
  });
});

describe("private IPv4 ranges (the backend's _is_private_net, Python 3.13)", () => {
  it("lists exactly the ranges the backend treats as private", () => {
    expect(PRIVATE_IPV4_RANGES).toEqual([
      "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
      "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "255.255.255.255/32",
    ]);
  });

  it("every edge of every range, the two exceptions and 100.64.0.0/10", () => {
    const inside = ["0.0.0.0", "0.255.255.255", "10.0.0.0", "10.255.255.255", "127.0.0.1", "169.254.0.0", "169.254.255.255", "172.16.0.0",
      "172.31.255.255", "192.0.0.0", "192.0.0.8", "192.0.0.11", "192.0.0.255", "192.0.2.0", "192.0.2.255", "192.168.0.0", "192.168.255.255",
      "198.18.0.0", "198.19.255.255", "198.51.100.0", "198.51.100.255", "203.0.113.0", "203.0.113.255", "240.0.0.0", "255.255.255.254", "255.255.255.255"];
    const outside = ["1.0.0.0", "9.255.255.255", "11.0.0.0", "126.255.255.255", "128.0.0.0", "169.253.255.255", "169.255.0.0", "172.15.255.255",
      "172.32.0.0", "191.255.255.255", "192.0.0.9", "192.0.0.10", "192.0.1.0", "192.0.3.0", "192.167.255.255", "192.169.0.0", "198.17.255.255",
      "198.20.0.0", "198.51.99.255", "198.51.101.0", "203.0.112.255", "203.0.114.0", "239.255.255.255", "100.64.0.0", "100.127.255.255", "8.8.8.8"];
    for (const address of inside) expect([address, isPrivateIPv4(address)]).toEqual([address, true]);
    for (const address of outside) expect([address, isPrivateIPv4(address)]).toEqual([address, false]);
  });

  it("a network is private only when it sits wholly inside one range and touches no exception", () => {
    expect(isPrivateIPv4("10.0.0.0/8")).toBe(true);
    expect(isPrivateIPv4("10.0.0.0/7")).toBe(false);
    expect(isPrivateIPv4("172.16.0.0/11")).toBe(false);
    expect(isPrivateIPv4("192.0.0.8/31")).toBe(false);
    expect(isPrivateIPv4("192.0.0.0/24")).toBe(true);
    expect(isPrivateIPv4("192.0.0.12/30")).toBe(true);
    expect(isPrivateIPv4("100.64.0.0/10")).toBe(false);
    expect(isPrivateIPv4("example.com")).toBe(false);
  });
});

describe("importJson", () => {
  const current = saved();

  it("takes an array of rules, replacing the staged rules and keeping default and strategy", () => {
    const result = importJson('[{"type":"port","value":"25","action":"block","label":"no SMTP"},{"type":"geoip","value":"cn","action":"direct","enabled":false}]', current);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.state.defaultAction).toBe("proxy");
    expect(result.state.domainStrategy).toBe("IPIfNonMatch");
    expect(result.state.rows.map(({ id, type, value, action, enabled, label }) => ({ id, type, value, action, enabled, label }))).toEqual([
      { id: null, type: "port", value: "25", action: "block", enabled: true, label: "no SMTP" },
      { id: null, type: "geoip", value: "cn", action: "direct", enabled: false, label: "" },
    ]);
    expect(new Set(result.state.rows.map((row) => row.key)).size).toBe(2);
  });

  it("takes {rules: []} and overwrites default and strategy only when present", () => {
    const withDefault = importJson('{"rules":[],"default_action":"block"}', current);
    expect(withDefault).toMatchObject({ ok: true, state: { rows: [], defaultAction: "block", domainStrategy: "IPIfNonMatch" } });
    const both = importJson('{"rules":[],"default_action":"direct","domain_strategy":"AsIs"}', current);
    expect(both).toMatchObject({ ok: true, state: { defaultAction: "direct", domainStrategy: "AsIs" } });
    expect(importJson(exportJson(current), resetRouting())).toMatchObject({ ok: true, state: { defaultAction: "proxy" } });
  });

  it("says invalid JSON when it does not parse", () => {
    expect(importJson("{rules:", current)).toEqual({ ok: false, error: "invalid JSON" });
    expect(importJson("", current)).toEqual({ ok: false, error: "invalid JSON" });
  });

  it("says no rules array / bad rule shape for anything else", () => {
    for (const text of ['{"default_action":"proxy"}', '"rules"', "null", "42", '{"rules":{}}', '[{"type":"dns","value":"x","action":"proxy"}]',
      '[{"type":"domain","value":"x","action":"reject"}]', '[{"type":"domain","action":"proxy"}]', '[{"type":"domain","value":"x","action":"proxy","enabled":"yes"}]',
      '[{"type":"domain","value":"x","action":"proxy","label":7}]', "[null]", '{"rules":[],"default_action":"drop"}', '{"rules":[],"domain_strategy":"Fast"}']) {
      expect([text, importJson(text, current)]).toEqual([text, { ok: false, error: "no rules array / bad rule shape" }]);
    }
  });
});

describe("testDestination (R8)", () => {
  const state = saved();

  it("nothing for an empty input; IPv6 is not evaluated locally", () => {
    expect(testDestination("   ", state)).toBeNull();
    expect(testDestination("2a00:1450:4010::65", state)).toEqual({ action: null, detail: "IPv6 preview is not evaluated locally — Validate/Save uses Xray's matcher." });
    expect(testDestination("[2a00:1450:4010::65]:443", state)?.action).toBeNull();
  });

  it("a private IPv4 goes direct before any rule, over the backend's whole private list", () => {
    for (const host of ["192.168.1.20", "10.0.0.1:443", "172.20.1.1", "127.0.0.1", "169.254.10.10", "198.18.5.5", "203.0.113.7:443", "240.1.2.3", "0.1.2.3"]) {
      expect(testDestination(host, state)).toEqual({ action: "direct", detail: "(private range, always matched first)" });
    }
    expect(testDestination("100.64.1.1", state)).toEqual({ action: "proxy", detail: "(default · geo rules not evaluated locally)" });
    expect(testDestination("192.0.0.9", state)?.detail).not.toContain("private");
  });

  it("domain rules match as Xray does: no prefix is a substring, so a literal '*.' token never matches", () => {
    // the fixture rule is domain "*.ya.ru, yandex.net" → direct; "*.ya.ru" is a literal substring no real host
    // contains, so ya.ru and mail.ya.ru fall through to the default, while "yandex.net" matches as a substring —
    // including inside "notyandex.net", which contains it.
    expect(testDestination("ya.ru", state)).toEqual({ action: "proxy", detail: "(default · geo rules not evaluated locally)" });
    expect(testDestination("mail.ya.ru:443", state)).toEqual({ action: "proxy", detail: "(default · geo rules not evaluated locally)" });
    expect(testDestination("yandex.net", state)).toEqual({ action: "direct", detail: '(matched domain "*.ya.ru, yandex.net")' });
    expect(testDestination("mail.yandex.net", state)?.action).toBe("direct");
    expect(testDestination("notyandex.net", state)?.action).toBe("direct");
  });

  it("honours domain: (name or subdomain), full: (exact) and keyword: (substring); regexp: is skipped like geo", () => {
    const domainRule = updateRule(addRule(state, "n-domain"), "n-domain", { type: "domain", value: "domain:example.org", action: "block" });
    expect(testDestination("example.org", domainRule)?.action).toBe("block");
    expect(testDestination("www.example.org", domainRule)?.action).toBe("block");
    expect(testDestination("notexample.org", domainRule)?.detail).toBe("(default · geo rules not evaluated locally)");

    const fullRule = updateRule(addRule(state, "n-full"), "n-full", { type: "domain", value: "full:example.org", action: "block" });
    expect(testDestination("example.org", fullRule)?.action).toBe("block");
    expect(testDestination("www.example.org", fullRule)?.detail).toBe("(default · geo rules not evaluated locally)");

    const keywordRule = updateRule(addRule(state, "n-keyword"), "n-keyword", { type: "domain", value: "keyword:ample", action: "block" });
    expect(testDestination("example.org", keywordRule)?.action).toBe("block");

    const regexpRule = updateRule(addRule(state, "n-regexp"), "n-regexp", { type: "domain", value: "regexp:^example", action: "block" });
    expect(testDestination("example.org", regexpRule)).toEqual({ action: "proxy", detail: "(default · geo rules not evaluated locally)" });
  });

  it("ip rules match IPv4 CIDRs and port rules a port, a range or a list; the label follows the match", () => {
    expect(testDestination("45.83.141.9:443", state)).toEqual({ action: "proxy", detail: '(matched ip "45.83.0.0/16" · office)' });
    expect(testDestination("mail.example.com:25", state)).toEqual({ action: "block", detail: '(matched port "25" · no SMTP)' });
    const ranged = updateRule(state, "s25", { value: "25, 1000-2000" });
    expect(testDestination("1.2.3.4:1500", ranged)?.detail).toBe('(matched port "25, 1000-2000" · no SMTP)');
    expect(testDestination("1.2.3.4:2001", ranged)?.action).toBe("proxy");
    expect(testDestination("1.2.3.4", ranged)?.detail).toBe("(default · geo rules not evaluated locally)");
  });

  it("ip tokens are read as the backend reads them: a stray '8.8.8.8/' or another malformed token matches nothing", () => {
    const withIp = (value: string) => updateRule(addRule(state, "n-ip"), "n-ip", { type: "ip", value, action: "block" });
    for (const value of ["8.8.8.8/", "8.8.8.8/33", "8.8.8.8/1/2", "08.8.8.8", "8.8.8.8/x"]) {
      expect(testDestination("1.2.3.4", withIp(value))?.action).toBe("proxy");
      expect(testDestination("8.8.8.8", withIp(value))?.action).toBe("proxy");
    }
    expect(testDestination("8.8.8.8", withIp("8.8.8.8"))?.action).toBe("block");
    expect(testDestination("8.8.8.9", withIp("8.8.8.8"))?.action).toBe("proxy");
    expect(testDestination("8.8.8.200:53", withIp("8.8.8.1/24"))?.action).toBe("block");
    expect(testDestination("8.8.9.1", withIp("8.8.8.0/24"))?.action).toBe("proxy");
    expect(testDestination("1.2.3.4", withIp("0.0.0.0/0"))?.action).toBe("block");
  });

  it("checks rules in order, first match wins, skipping switched-off and empty rules", () => {
    expect(testDestination("netflix.com", state)?.detail).toBe("(default · geo rules not evaluated locally)");
    const on = updateRule(state, "s26", { enabled: true });
    expect(testDestination("netflix.com", on)).toEqual({ action: "proxy", detail: '(matched domain "netflix.com")' });
    const first = moveRule(moveRule(moveRule(updateRule(on, "s26", { action: "block" }), "s26", -1), "s26", -1), "s26", -1);
    const shadow = updateRule(addRule(first, "n-x"), "n-x", { value: "netflix.com", action: "direct" });
    expect(testDestination("netflix.com", shadow)?.action).toBe("block");
    const emptied = updateRule(state, "s24", { value: "" });
    expect(testDestination("45.83.141.9", emptied)?.detail).toBe("(default · geo rules not evaluated locally)");
  });

  it("no match falls to the default action; the geo note only when a geo rule was skipped", () => {
    const noGeo = removeRule(removeRule(state, "s21"), "s22");
    expect(testDestination("example.org", { ...noGeo, defaultAction: "block" })).toEqual({ action: "block", detail: "(default)" });
    expect(testDestination("example.org", { ...state, defaultAction: "direct" })).toEqual({ action: "direct", detail: "(default · geo rules not evaluated locally)" });
  });
});

describe("constants", () => {
  it("geo suggestions and value placeholders, word for word", () => {
    expect(GEO_TOKENS).toEqual(["ru", "cn", "private", "category-ru", "category-ads-all", "geolocation-!cn", "google", "telegram"]);
    expect(VALUE_PLACEHOLDERS).toEqual({
      geoip: "ru | private | cn (comma-sep ok)", geosite: "category-ads-all", domain: "example.com, domain:ya.ru",
      ip: "1.2.3.0/24, 10.0.0.0/8", port: "443 | 1000-2000 | 80,443",
    });
    expect(keyOf(saved(), "ru")).toBe("s22");
  });
});
