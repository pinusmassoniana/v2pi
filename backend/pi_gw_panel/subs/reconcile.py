import hashlib
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


def _shrink_fingerprint(parsed: list[Node]) -> str:
    identities = repr(sorted(_identity(node) for node in parsed)).encode("utf-8")
    return hashlib.sha256(identities).hexdigest()


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
    # Bound untrusted strings at the single reconcile choke point, then make the complete merge
    # one SQLite transaction. Store mutator commit calls are transaction-aware no-ops.
    parsed = _dedupe([clamp_node_fields(p) for p in parsed])
    added = updated = removed = skipped_deletes = 0
    active_changed = active_vanished = False
    try:
        with store.transaction():
            existing = store.list_nodes_for_sub(sub_id)
            anomalous_shrink = bool(parsed) and len(existing) > 0 and len(parsed) < len(existing) * 0.5
            shrink_key = f"subscription_shrink:{sub_id}"
            fingerprint = _shrink_fingerprint(parsed) if anomalous_shrink else ""
            shrink_confirmed = anomalous_shrink and store.get_setting(shrink_key) == fingerprint
            store.set_setting(shrink_key, fingerprint)
            seen: set[tuple] = set()
            for pos, p in enumerate(parsed):
                key = _identity(p)
                seen.add(key)
                cur = store.get_node_by_identity(sub_id, *key)
                p.subscription_id = sub_id
                p.stale = False
                p.position = pos
                if cur is None:
                    p.id = None
                    p.tuning_profile_id = default_profile_id
                    store.add_node(p)
                    added += 1
                else:
                    p.id = cur.id
                    p.tuning_profile_id = cur.tuning_profile_id
                    if cur.id == active_node_id and _config_differs(cur, p):
                        active_changed = True
                    store.update_node(p)  # also clears a prior first-shrink stale marker
                    updated += 1
            for n in existing:
                if _identity(n) in seen:
                    continue
                if not parsed:
                    skipped_deletes += 1
                elif anomalous_shrink and (not shrink_confirmed or not n.stale):
                    store.mark_stale(n.id, True)
                    skipped_deletes += 1
                elif n.id == active_node_id:
                    store.mark_stale(n.id, True)
                    active_vanished = True
                else:
                    store.delete_node(n.id)
                    removed += 1
    except Exception:
        log.exception("reconcile: atomic store merge failed for sub %s", sub_id)
        raise
    if skipped_deletes:
        log.warning("reconcile: sub %s returned %d node(s) vs %d stored — refused to delete %d "
                    "(awaiting one matching confirmation)", sub_id, len(parsed), len(existing),
                    skipped_deletes)
    active_replacement = None
    if active_vanished:
        fresh = [n for n in store.list_nodes_for_sub(sub_id) if not n.stale]
        if len(fresh) == 1:                       # single-server sub that rotated its identity
            active_replacement = fresh[0].id
    return {"added": added, "updated": updated, "removed": removed,
            "active_changed": active_changed, "active_replacement": active_replacement,
            "skipped_deletes": skipped_deletes}
