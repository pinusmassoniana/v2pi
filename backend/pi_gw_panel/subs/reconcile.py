from pi_gw_panel.models import Node
from pi_gw_panel.nodes.store import NodeStore


def reconcile(store: NodeStore, sub_id: int, parsed: list[Node],
              active_node_id: int | None) -> dict:
    """Merge parsed nodes into the store under sub_id, matching by (address, port, uuid):
    update changed, add new, remove vanished — EXCEPT the active node is never removed
    (flagged stale instead) so a live connection survives. Returns add/update/remove counts."""
    existing = store.list_nodes_for_sub(sub_id)
    seen: set[tuple] = set()
    added = updated = removed = 0
    for pos, p in enumerate(parsed):
        key = (p.address, p.port, p.uuid, p.path)
        seen.add(key)
        cur = store.get_node_by_identity(*key)
        p.subscription_id = sub_id
        p.stale = False
        p.position = pos
        if cur is None:
            p.id = None
            store.add_node(p)
            added += 1
        else:
            p.id = cur.id
            store.update_node(p)
            updated += 1
    for n in existing:
        if (n.address, n.port, n.uuid, n.path) not in seen:
            if n.id == active_node_id:
                store.mark_stale(n.id, True)  # protect the live connection
            else:
                store.delete_node(n.id)
                removed += 1
    return {"added": added, "updated": updated, "removed": removed}
