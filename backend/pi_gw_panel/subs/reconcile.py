from pi_gw_panel.models import Node
from pi_gw_panel.nodes.store import NodeStore

# Fields that change the generated xray config — a change to any of these on the active
# node means the live tunnel must be re-applied to pick the new value up.
_CONFIG_FIELDS = ("address", "port", "uuid", "transport", "sni", "public_key", "short_id",
                  "fingerprint", "flow", "network", "security", "path", "host", "mode", "alpn")


def _config_differs(a: Node, b: Node) -> bool:
    return any(getattr(a, f) != getattr(b, f) for f in _CONFIG_FIELDS)


def reconcile(store: NodeStore, sub_id: int, parsed: list[Node],
              active_node_id: int | None) -> dict:
    """Merge parsed nodes into the store under sub_id, matching by (address, port, uuid, path):
    update changed, add new, remove vanished — EXCEPT the active node is never removed
    (flagged stale instead) so a live connection survives.

    Also reports how the active node was affected, so the caller can restart the tunnel on
    the refreshed server:
    - ``active_changed``: the active node stayed (same identity) but a config field changed
      (e.g. the reality key/sni rotated) → re-apply it.
    - ``active_replacement``: the active node vanished (its identity rotated) and the sub now
      has exactly one fresh node — that node's id, the single server to move the connection to.
    """
    existing = store.list_nodes_for_sub(sub_id)
    seen: set[tuple] = set()
    added = updated = removed = 0
    active_changed = active_vanished = False
    for pos, p in enumerate(parsed):
        key = (p.address, p.port, p.uuid, p.path)
        seen.add(key)
        cur = store.get_node_by_identity(sub_id, *key)
        p.subscription_id = sub_id
        p.stale = False
        p.position = pos
        if cur is None:
            p.id = None
            store.add_node(p)
            added += 1
        else:
            p.id = cur.id
            if cur.id == active_node_id and _config_differs(cur, p):
                active_changed = True
            store.update_node(p)
            updated += 1
    for n in existing:
        if (n.address, n.port, n.uuid, n.path) not in seen:
            if n.id == active_node_id:
                store.mark_stale(n.id, True)  # protect the live connection
                active_vanished = True
            else:
                store.delete_node(n.id)
                removed += 1
    active_replacement = None
    if active_vanished:
        fresh = [n for n in store.list_nodes_for_sub(sub_id) if not n.stale]
        if len(fresh) == 1:                       # single-server sub that rotated its identity
            active_replacement = fresh[0].id
    return {"added": added, "updated": updated, "removed": removed,
            "active_changed": active_changed, "active_replacement": active_replacement}
