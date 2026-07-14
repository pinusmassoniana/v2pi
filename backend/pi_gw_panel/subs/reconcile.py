import logging
from pi_gw_panel.models import Node
from pi_gw_panel.nodes.store import NodeStore
from pi_gw_panel.subs.parsers import clamp_node_fields

log = logging.getLogger("pi_gw_panel")

# Fields that change the generated xray config — a change to any of these on the active
# node means the live tunnel must be re-applied to pick the new value up.
_CONFIG_FIELDS = ("address", "port", "uuid", "transport", "sni", "public_key", "short_id",
                  "fingerprint", "flow", "network", "security", "path", "host", "mode", "alpn")


def _config_differs(a: Node, b: Node) -> bool:
    return any(getattr(a, f) != getattr(b, f) for f in _CONFIG_FIELDS)


def _identity(n: Node) -> tuple:
    # sni/short_id are part of the key so a reality feed presenting many concurrent exit configs
    # on one IP:port (same uuid, differing only by SNI/shortId) keeps each as a distinct node
    # instead of collapsing 51 advertised locations down to the handful of shared endpoints.
    return (n.address, n.port, n.uuid, n.path, n.sni, n.short_id)


def _dedupe(parsed: list[Node]) -> list[Node]:
    """Collapse entries with the same identity — last wins, first-seen order —
    so a feed that lists the same server twice yields one node (and honest counts)."""
    by_key: dict[tuple, Node] = {}
    for p in parsed:
        by_key[_identity(p)] = p
    return list(by_key.values())


def reconcile(store: NodeStore, sub_id: int, parsed: list[Node],
              active_node_id: int | None, default_profile_id: int | None = None) -> dict:
    """Merge parsed nodes into the store under sub_id, matching by node identity
    (address, port, uuid, path, sni, short_id):
    update changed, add new, remove vanished — EXCEPT the active node is never removed
    (flagged stale instead) so a live connection survives.

    User-owned per-node state is preserved across a refresh: an updated node keeps its
    assigned ``tuning_profile_id`` (the feed never carries one), and a *new* node inherits the
    subscription's ``default_profile_id`` when one is set.

    Also reports how the active node was affected, so the caller can restart the tunnel on
    the refreshed server:
    - ``active_changed``: the active node stayed (same identity) but a config field changed
      (e.g. the reality key/sni rotated) → re-apply it.
    - ``active_replacement``: the active node vanished (its identity rotated) and the sub now
      has exactly one fresh node — that node's id, the single server to move the connection to.
    """
    existing = store.list_nodes_for_sub(sub_id)
    # P2: bound untrusted feed string fields at this single choke point (every refreshed node
    # flows through here) before they reach the store — see clamp_node_fields.
    parsed = _dedupe([clamp_node_fields(p) for p in parsed])
    # P1: a transient error page can parse to an empty/near-empty list; deleting every vanished
    # node then wipes the sub on one bad response. Refuse the delete pass when the feed is empty
    # or shrank by >50% vs the stored count — adds/updates still run; the (harmless, prunable)
    # extras are cleaned up by the next healthy refresh.
    skip_deletes = not parsed or (len(existing) > 0 and len(parsed) < len(existing) * 0.5)
    seen: set[tuple] = set()
    added = updated = removed = skipped_deletes = 0
    active_changed = active_vanished = False
    # reconcile is NOT a single transaction (each store op commits on its own — the store's
    # mutators don't expose a shared boundary); run adds/updates before deletes so a store
    # error mid-loop leaves extra nodes rather than a wiped sub (P2, least-harmful ordering).
    try:
        for pos, p in enumerate(parsed):
            key = _identity(p)
            seen.add(key)
            cur = store.get_node_by_identity(sub_id, *key)
            p.subscription_id = sub_id
            p.stale = False
            p.position = pos
            if cur is None:
                p.id = None
                p.tuning_profile_id = default_profile_id   # N5: inherit the sub's default profile
                store.add_node(p)
                added += 1
            else:
                p.id = cur.id
                p.tuning_profile_id = cur.tuning_profile_id  # C2: keep the user's per-node choice
                if cur.id == active_node_id and _config_differs(cur, p):
                    active_changed = True
                store.update_node(p)
                updated += 1
        for n in existing:
            if _identity(n) not in seen:
                if skip_deletes:
                    skipped_deletes += 1        # P1: don't trust an empty/near-empty response
                    continue
                if n.id == active_node_id:
                    store.mark_stale(n.id, True)  # protect the live connection
                    active_vanished = True
                else:
                    store.delete_node(n.id)
                    removed += 1
    except Exception:
        log.exception("reconcile: store error mid-reconcile for sub %s (partial state possible)",
                      sub_id)
        raise
    if skipped_deletes:
        log.warning("reconcile: sub %s returned %d node(s) vs %d stored — refused to delete %d "
                    "(possible transient/empty feed)", sub_id, len(parsed), len(existing),
                    skipped_deletes)
    active_replacement = None
    if active_vanished:
        fresh = [n for n in store.list_nodes_for_sub(sub_id) if not n.stale]
        if len(fresh) == 1:                       # single-server sub that rotated its identity
            active_replacement = fresh[0].id
    return {"added": added, "updated": updated, "removed": removed,
            "active_changed": active_changed, "active_replacement": active_replacement,
            "skipped_deletes": skipped_deletes}
