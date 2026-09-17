import { queryOptions } from "@tanstack/react-query";
import { api } from "./client";
import { recordServerNow } from "./clock";

export const keys = {
  status: ["status"] as const,
  nodes: ["nodes"] as const,
  nodeHealth: ["nodeHealth"] as const,
  subs: ["subs"] as const,
  profiles: ["profiles"] as const,
  profilePresets: ["profilePresets"] as const,
  routing: ["routing"] as const,
  routingPresets: ["routingPresets"] as const,
  network: ["network"] as const,
  rw: ["rw"] as const,
  settings: ["settings"] as const,
  tokens: ["tokens"] as const,
  diagnostics: ["diagnostics"] as const,
  geo: ["geo"] as const,
  events: ["events"] as const,
  trafficUsage: ["trafficUsage"] as const,
  reservations: ["reservations"] as const,
  audit: ["audit"] as const,
  logs: (source: string, lines: number) => ["logs", source, lines] as const,
  trafficHistory: (windowSec: number) => ["trafficHistory", windowSec] as const,
};

const HISTORY_MAX_POINTS = 1200;

export const queries = {
  status: () =>
    queryOptions({
      queryKey: keys.status,
      queryFn: async ({ signal }) => {
        const status = await api.getStatus(signal);
        recordServerNow(status.server_now);
        return status;
      },
      // The offline banner reacts to the first failed poll, not after retries.
      retry: false,
    }),
  nodes: () => queryOptions({ queryKey: keys.nodes, queryFn: () => api.listNodes() }),
  nodeHealth: () => queryOptions({ queryKey: keys.nodeHealth, queryFn: () => api.listNodeHealth() }),
  subs: () => queryOptions({ queryKey: keys.subs, queryFn: () => api.listSubs() }),
  profiles: () => queryOptions({ queryKey: keys.profiles, queryFn: () => api.listProfiles() }),
  profilePresets: () => queryOptions({ queryKey: keys.profilePresets, queryFn: () => api.listProfilePresets() }),
  routing: () => queryOptions({ queryKey: keys.routing, queryFn: () => api.getRouting() }),
  routingPresets: () => queryOptions({ queryKey: keys.routingPresets, queryFn: () => api.listRoutingPresets() }),
  // The signal aborts a GET that a write's invalidation cancelled, so it can never land over the write's reply.
  network: () => queryOptions({ queryKey: keys.network, queryFn: ({ signal }) => api.getNetwork(signal) }),
  rw: () => queryOptions({ queryKey: keys.rw, queryFn: () => api.getRw() }),
  settings: () => queryOptions({ queryKey: keys.settings, queryFn: () => api.getSettings() }),
  tokens: () => queryOptions({ queryKey: keys.tokens, queryFn: () => api.listTokens() }),
  diagnostics: () => queryOptions({ queryKey: keys.diagnostics, queryFn: () => api.getDiagnostics() }),
  geo: () => queryOptions({ queryKey: keys.geo, queryFn: () => api.getGeo() }),
  events: (windowSec: number, kind = "") => queryOptions({ queryKey: [...keys.events, windowSec, kind], queryFn: () => api.listEvents(windowSec, kind) }),
  trafficUsage: () => queryOptions({ queryKey: keys.trafficUsage, queryFn: () => api.getTrafficUsage() }),
  reservations: () => queryOptions({ queryKey: keys.reservations, queryFn: () => api.listReservations() }),
  audit: () => queryOptions({ queryKey: keys.audit, queryFn: () => api.listAudit() }),
  logs: (source: string, lines: number) =>
    queryOptions({ queryKey: keys.logs(source, lines), queryFn: () => api.getLogs(source, lines) }),
  trafficHistory: (windowSec: number) =>
    queryOptions({
      queryKey: keys.trafficHistory(windowSec),
      queryFn: ({ signal }) => api.getTrafficHistory(windowSec, HISTORY_MAX_POINTS, signal),
    }),
};
