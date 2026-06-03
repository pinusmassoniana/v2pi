from pi_gw_panel.models import Node, TuningProfile


def resolve_profile(store, node: Node) -> TuningProfile | None:
    """Resolve the tuning profile that governs `node`: its explicitly assigned
    profile, else the global default, else None.

    `None` (no store, or no default seeded) is the Wave-0 path — `build_config`
    with `profile=None` stays byte-identical to Wave-0."""
    if store is None:
        return None
    if node.tuning_profile_id is not None:
        p = store.get_profile(node.tuning_profile_id)
        if p is not None:
            return p
        # dangling reference (profile deleted) → fall through to the default
    return store.get_default_profile()
