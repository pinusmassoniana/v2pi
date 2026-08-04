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


# How much protection a transport security offers. A feed-driven change to the live tunnel may
# move *up* this ladder or stay level, never down — a feed must not be able to walk the live
# tunnel to a weaker mode, whether by offering a replacement node or by rewriting the active
# node in place.
_SECURITY_RANK = {"tls": 1, "reality": 2}


def security_kept(old, new) -> bool:
    """True when `new` protects at least as well as `old` on the transport-security ladder.
    Unknown/absent values rank 0, so anything → unknown is a downgrade and unknown → anything
    is not. A missing node on either side is not something to judge, so it passes.

    Lives here rather than in `service` because the ladder has to be enforced at the point the
    weakened row would be *written* (below), and `service` already imports this module."""
    if old is None or new is None:
        return True
    return _SECURITY_RANK.get(new.security, 0) >= _SECURITY_RANK.get(old.security, 0)


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
    assigned ``tuning_profile_id`` and its operator ``note`` (the feed carries neither), and a
    *new* node inherits the subscription's ``default_profile_id`` when one is set.

    Also reports how the active node was affected, so the caller can restart the tunnel on
    the refreshed server:
    - ``active_changed``: the active node stayed (same identity) but a config field changed
      (e.g. the reality key/sni rotated) → re-apply it.
    - ``active_replacement``: the active node vanished (its identity rotated) and the sub now
      has exactly one fresh node — that node's id, the single server to move the connection to.
    - ``active_downgrade``: the feed re-advertised the active node's identity with a *weaker*
      transport security. That one row is left exactly as stored (see below) and the caller
      fails the refresh; the pair ``(stored_security, offered_security)`` is reported so it can
      say what was refused.
    """
    # Bound untrusted strings at the single reconcile choke point, then make the complete merge
    # one SQLite transaction. Store mutator commit calls are transaction-aware no-ops.
    parsed = _dedupe([clamp_node_fields(p) for p in parsed])
    added = updated = removed = skipped_deletes = 0
    active_changed = active_vanished = False
    active_downgrade: tuple[str, str] | None = None
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
                    # user-owned columns the feed never carries: update_node writes the whole
                    # row, so anything not carried over here is wiped on every refresh.
                    p.tuning_profile_id = cur.tuning_profile_id
                    p.note = cur.note
                    if cur.id == active_node_id and not security_kept(cur, p):
                        # The security ladder has to be enforced *here*, where the weakened row
                        # would be written — not after the merge. Every store mutator commits,
                        # so a row written "to be restored by the caller" is durable and
                        # readable the instant it lands: a concurrent manual apply (which reads
                        # the node before taking its own lock) could capture and apply the
                        # downgrade, and a restore that itself fails would park the weakened
                        # config for the next Connect. So refuse the feed's version of this one
                        # row and leave the stored node untouched — not its config, not its
                        # position, not its stale flag. The rest of the merge still lands, and
                        # the caller turns the subscription red.
                        active_downgrade = (cur.security, p.security)
                        log.warning(
                            "reconcile: sub %s re-advertised the active node %s with weaker "
                            "security=%s (stored security=%s) — refusing to write the row",
                            sub_id, cur.id, p.security, cur.security)
                        continue
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
            "skipped_deletes": skipped_deletes, "active_downgrade": active_downgrade}
