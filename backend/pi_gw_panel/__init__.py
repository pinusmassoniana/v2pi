from importlib.metadata import distributions


def _installed_version() -> str:
    """Return the installed package version even in a partially-corrupt dev environment.

    Old editable installs can leave metadata directories without a ``Version`` field.  In
    that case ``importlib.metadata.version()`` currently returns ``None`` instead of raising,
    even though a newer, valid distribution is present beside it.  Production images have a
    single distribution, while this fallback keeps diagnostics truthful during upgrades too.
    """
    for distribution in distributions(name="pi-gw-panel"):
        detected = distribution.metadata.get("Version")
        if detected:
            return detected
    return "0+uninstalled"


__version__ = _installed_version()
