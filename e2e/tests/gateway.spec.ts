import { expect, test, type Page } from "@playwright/test";
import { ensureLoggedIn } from "./auth-helper";

// The e2e backend runs the dry-run network backend (run-server.sh forces PI_GW_NET_BACKEND=dryrun): host provisioning
// renders and returns, nothing is applied to this machine, and xray is /bin/true, so no node is ever connected — an
// Apply asks nothing and a remote-access revocation finds xray stopped. Every test starts and ends with the network as
// the fresh gateway had it, gateway DNS off, remote access at its defaults and no e2e-* client: the other specs expect
// a fresh gateway. A private key, once stored, cannot be cleared through the API, so these pass with or without one.

const PREFIX = "e2e-";
// Shaped like `xray x25519` output — 32 bytes as unpadded base64url — built from the byte ramps 0x20–0x3F and
// 0x00–0x1F the backend's tests use rather than pasted, so they are fake on sight to a reader and the secret scan alike.
const byteRamp = (first: number) => Buffer.from(Array.from({ length: 32 }, (_, i) => first + i)).toString("base64url");
const PUBLIC_KEY = byteRamp(0x20);
const PRIVATE_KEY = byteRamp(0x00);

interface NetworkRead {
  segment: Record<"iface" | "ip" | "ip6" | "dhcp_start" | "dhcp_end" | "dhcp_lease" | "client_dns" | "client_dns6", string>;
  kill_switch_enabled: boolean;
  lan_access_enabled: boolean;
  ipv6_enabled: boolean;
  status: { uplink: boolean | null };
}

let fresh: NetworkRead | null = null;

async function gateway(page: Page) {
  const csrf = ((await (await page.request.get("/api/csrf")).json()) as { csrf: string }).csrf;
  const headers = { "X-CSRF-Token": csrf };
  return {
    get: async <T>(path: string) => (await (await page.request.get(`/api${path}`)).json()) as T,
    put: (path: string, data: unknown) => page.request.put(`/api${path}`, { data, headers }),
    remove: (path: string) => page.request.delete(`/api${path}`, { headers }),
  };
}

async function cleanUp(page: Page) {
  const api = await gateway(page);
  const network = await api.get<NetworkRead>("/network");
  // The belt under run-server.sh's braces: the dry-run backend never probes the uplink. Anything else is a real host.
  expect(network.status.uplink, "the e2e backend must be the dry-run network backend").toBeNull();
  fresh ??= network;
  const { segment } = fresh;
  const restored = await api.put("/network", {
    segment_iface: segment.iface, segment_ip: segment.ip, segment_ip6: segment.ip6, dhcp_start: segment.dhcp_start, dhcp_end: segment.dhcp_end,
    dhcp_lease: segment.dhcp_lease, client_dns: segment.client_dns, client_dns6: segment.client_dns6,
    kill_switch_enabled: fresh.kill_switch_enabled, lan_access_enabled: fresh.lan_access_enabled, ipv6_enabled: fresh.ipv6_enabled,
  });
  expect(restored.ok()).toBe(true);
  await api.put("/settings", { dns_intercept: false });
  for (const client of (await api.get<{ clients: { id: string; email: string }[] }>("/rw")).clients) {
    if (client.email.startsWith(PREFIX)) await api.remove(`/rw/clients/${client.id}`);
  }
  // PUT /rw replaces everything: omitted fields reset, so the fresh gateway's dest and server name are sent back.
  await api.put("/rw", { dest: "www.microsoft.com:443", server_names: "www.microsoft.com" });
}

test.beforeEach(async ({ page }) => {
  await ensureLoggedIn(page);
  await cleanUp(page);
});

test.afterEach(async ({ page }) => {
  await cleanUp(page);
});

test("Network: a refused interface and pool, an apply that sticks, the kill-switch disarmed and re-armed, gateway DNS", async ({ page }, info) => {
  const phone = info.project.name === "phone";
  await page.goto("/#/gateway/network");
  await page.reload();   // the network was restored behind the screen's back: load it afresh
  const kill = page.getByRole("region", { name: "Kill-switch" });
  await expect(kill.getByText("ARMED", { exact: true })).toBeVisible();
  await expect(page.getByText("No active leases.")).toBeVisible();
  if (phone) {
    await page.getByRole("button", { name: /^Gateway Segment/ }).click();
    await page.getByRole("button", { name: /^Router checklist/ }).click();
    await expect(page.getByRole("button", { name: "Create VLAN 2" })).toBeVisible();
  } else {
    const steps = page.getByRole("region", { name: "Router checklist" }).getByRole("listitem");
    await expect(steps.first()).toContainText("1Create VLAN 2");
  }
  const apply = phone ? page.getByRole("button", { name: "Apply to host", exact: true }) : page.getByRole("region", { name: "Gateway Segment" }).getByRole("button", { name: "Apply to host" });

  const iface = page.getByLabel("Segment interface");
  await iface.fill("eth 0");
  await expect(page.getByText("segment_iface: must be a plain interface name")).toBeVisible();
  await expect(apply).toBeDisabled();
  await iface.fill("eth0.2");

  const end = page.getByLabel("DHCP range end");
  await end.fill("192.168.10.20");
  await expect(page.getByText("DHCP pool must sit inside 192.168.10.0/24 with start ≤ end")).toBeVisible();
  await expect(page.getByText("✕ DHCP pool invalid")).toBeVisible();
  await expect(apply).toBeDisabled();
  await end.fill("192.168.10.150");
  await expect(apply).toBeEnabled();
  const sent = page.waitForResponse((response) => response.url().endsWith("/api/network") && response.request().method() === "PUT");
  await apply.click();
  expect((await sent).request().postDataJSON()).toMatchObject({ dhcp_end: "192.168.10.150", segment_iface: "eth0.2", kill_switch_enabled: true });
  await expect(page.getByText("saved · network + DHCP applied to host")).toBeVisible();
  await page.reload();
  if (phone) await page.getByRole("button", { name: /^Gateway Segment/ }).click();
  await expect(page.getByLabel("DHCP range end")).toHaveValue("192.168.10.150");

  await kill.getByRole("switch", { name: "Fail-closed kill-switch" }).click();
  const ask = page.getByRole("dialog", { name: "Confirm" });
  await expect(ask).toContainText("Disarm the fail-closed kill-switch?");
  await ask.getByRole("button", { name: "Disarm" }).click();
  await expect(kill.getByText("will disarm on Apply")).toBeVisible();
  await expect(kill.getByText("ARMED", { exact: true })).toBeVisible();
  await (phone ? apply : kill.getByRole("button", { name: "Apply to host" })).click();
  await expect(kill.getByText("OPEN", { exact: true })).toBeVisible();
  await expect(page.getByRole("group", { name: "WAN blocked" })).toHaveCount(0);

  await kill.getByRole("switch", { name: "Fail-closed kill-switch" }).click();   // arming asks nothing
  await expect(page.getByRole("dialog", { name: "Confirm" })).toHaveCount(0);
  await (phone ? apply : kill.getByRole("button", { name: "Apply to host" })).click();
  await expect(kill.getByText("ARMED", { exact: true })).toBeVisible();
  // With no tunnel up, the armed guard holds the WAN — confirmed on the (dry-run) host.
  await expect(page.getByRole("group", { name: "WAN blocked" })).toBeVisible();

  const dns = page.getByRole("switch", { name: "Gateway DNS" });
  await expect(dns).toHaveAttribute("aria-checked", "false");
  await dns.click();
  await expect(page.getByText("saved — applies on next Connect")).toBeVisible();
  await expect(dns).toHaveAttribute("aria-checked", "true");
  await dns.click();
  await expect(dns).toHaveAttribute("aria-checked", "false");
});

test("Remote access: set up and enable the inbound, add a device, reveal it, download its .conf, copy its link, suspend and remove it", async ({ page, context }, info) => {
  const phone = info.project.name === "phone";
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/#/gateway/remote-access");
  await page.reload();
  await expect(page.getByText("No clients yet.")).toBeVisible();

  await page.getByLabel("External endpoint").fill("home.example.org");
  await page.getByLabel("Public key", { exact: true }).fill(PUBLIC_KEY);
  await page.getByLabel("Private key").fill(PRIVATE_KEY);
  await page.getByRole("button", { name: "Generate" }).click();
  await expect(page.getByText("short id added — save to apply")).toBeVisible();
  await expect(page.getByRole("list", { name: "Short ids list" }).getByRole("listitem")).toHaveCount(1);
  const enable = page.getByRole("switch", { name: "Accept inbound connections" });
  await enable.click();
  await expect(enable).toHaveAttribute("aria-checked", "true");
  const saved = page.waitForResponse((response) => response.url().endsWith("/api/rw") && response.request().method() === "PUT");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const body = (await saved).request().postDataJSON() as Record<string, unknown>;
  expect(Object.keys(body).sort()).toEqual(["dest", "enabled", "endpoint", "hosts", "port", "private_key", "public_key", "routed_nets", "server_names", "short_ids"]);
  expect(body).toMatchObject({ enabled: true, endpoint: "home.example.org", public_key: PUBLIC_KEY, server_names: "www.microsoft.com" });
  await expect(page.getByText("saved · no active node right now, so it applies on the next connect")).toBeVisible();
  await expect(page.getByLabel("Private key")).toHaveValue("");
  await expect(page.getByRole("group", { name: "Enabled with no clients" })).toBeVisible();

  if (phone) {
    await page.getByRole("button", { name: "Add client" }).click();
    await page.getByRole("dialog", { name: "Add client" }).getByLabel("Client name").fill("e2e-phone");
    await page.getByRole("dialog", { name: "Add client" }).getByLabel("Client name").press("Enter");
  } else {
    await page.getByPlaceholder("iphone").fill("e2e-phone");
    await page.getByPlaceholder("iphone").press("Enter");
  }
  await expect(page.getByText("added e2e-phone")).toBeVisible();
  const device = phone ? page.getByRole("listitem", { name: "e2e-phone" }) : page.getByRole("row", { name: /^e2e-phone/ });
  await expect(device.getByText("uuid hidden")).toBeAttached();
  const uuid = /[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/;
  await expect(device).not.toContainText(uuid);
  await device.getByRole("button", { name: "Reveal uuid of e2e-phone" }).click();
  await expect(device).toContainText(uuid);
  await expect(page.getByRole("group", { name: "Enabled with no clients" })).toHaveCount(0);

  const download = page.waitForEvent("download");
  await device.getByRole("button", { name: "Download .conf for e2e-phone" }).click();
  expect((await download).suggestedFilename()).toBe("e2e-phone.conf");
  await expect(page.getByText("e2e-phone.conf downloaded — import it in Shadowrocket")).toBeVisible();

  await device.getByRole("button", { name: "Copy link for e2e-phone" }).click();
  await expect(page.getByText("vless:// link copied")).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toMatch(/^vless:\/\/[0-9a-f-]{36}@home\.example\.org:443\?.*#e2e-phone$/);
  await expect(page.getByText(/^vless:\/\/[0-9a-f]/)).toHaveCount(0);

  // No tunnel was ever up, so each revocation finds xray stopped and says so.
  if (phone) {
    await device.getByRole("button", { name: "More actions for e2e-phone" }).click();
    await page.getByRole("menuitem", { name: "Suspend" }).click();
  } else {
    await device.getByRole("button", { name: "Suspend e2e-phone" }).click();
  }
  await expect(page.getByText("client suspended — its uuid is kept — xray stopped — remote access is down for everyone until you reconnect")).toBeVisible();
  await expect(device).toContainText("suspended");

  if (phone) {
    await device.getByRole("button", { name: "More actions for e2e-phone" }).click();
    await page.getByRole("menuitem", { name: "Remove…" }).click();
  } else {
    await device.getByRole("button", { name: "Remove e2e-phone" }).click();
  }
  const ask = page.getByRole("dialog", { name: "Confirm" });
  await expect(ask).toContainText("Remove e2e-phone? Its link and config stop working immediately.");
  await ask.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("removed e2e-phone — xray stopped — remote access is down for everyone until you reconnect")).toBeVisible();
  await expect(page.getByText("No clients yet.")).toBeVisible();
});
