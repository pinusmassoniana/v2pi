"""A3: the routing data files, and replacing them without breaking the live tunnel.

The image bakes the xray release's `geoip.dat` / `geosite.dat` into `/usr/local/bin` (Dockerfile),
where nothing can ever update them: the lists a gateway routes Russian traffic by were frozen on
the day the image was built. This module moves them into `<data_dir>/geo`, points xray there with
`XRAY_LOCATION_ASSET`, and can replace them from their upstreams.

Two datasets live side by side, which is what keeps both vocabularies available at once:

  * `stock` — the v2fly files under their default names, so `geoip:ru` / `geosite:category-ads-all`
    keep working exactly as before;
  * `ru`    — runetfreedom's files under their own names, addressed as
    `ext:geosite_ru.dat:ru-blocked`, which is where the Russian blocklists live.

Updating is manual (owner decision, 2026-09-17): a swap only takes effect when xray reloads, and
an unattended reload is a connection drop nobody asked for. So the operator presses a button, and
this module does the careful part:

    download → verify the published sha256 → stage → `xray -test` the LIVE config against the
    staged files → swap → reload → if xray does not come back, put the old files back and reload
    again.

`-test` against the running config is the load-bearing step: a dataset that no longer carries a
category some rule names would otherwise take the tunnel down on the next restart, hours later,
with nothing pointing at the update as the cause.
"""
import logging
import os
import shutil
import tempfile
from hashlib import sha256

from pi_gw_panel.subs.fetcher import fetch_url
from pi_gw_panel.xray_config.validate import explain_xray_error, validate_config

log = logging.getLogger(__name__)

ASSET_ENV = "XRAY_LOCATION_ASSET"
IMAGE_ASSET_DIR = "/usr/local/bin"      # where the Dockerfile unzips the release's dat files
_SUFFIX_PREV = ".prev"
_DOWNLOAD_TIMEOUT = 120.0
# geosite_ru.dat is ~74 MB today. The cap is generous but finite: an upstream that starts serving
# an HTML error page or a 2 GB file must not fill the gateway's disk.
MAX_FILE_BYTES = 256 * 1024 * 1024
_RELEASE = "https://github.com/{repo}/releases/latest/download/{asset}"

# dataset → (local file name, upstream repo, asset name). The stock files keep xray's default
# names on purpose: every existing `geoip:` / `geosite:` rule resolves through them.
DATASETS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "stock": (
        ("geoip.dat", "v2fly/geoip", "geoip.dat"),
        # v2fly publishes the domain list as dlc.dat; xray reads it as geosite.dat.
        ("geosite.dat", "v2fly/domain-list-community", "dlc.dat"),
    ),
    "ru": (
        ("geoip_ru.dat", "runetfreedom/russia-v2ray-rules-dat", "geoip.dat"),
        ("geosite_ru.dat", "runetfreedom/russia-v2ray-rules-dat", "geosite.dat"),
    ),
}
# Which file an `ext:` reference names, per dataset — the one place routing and the UI agree on it.
EXT_FILES = {"ru": {"geoip": "geoip_ru.dat", "geosite": "geosite_ru.dat"}}


def geo_dir(settings) -> str:
    return os.path.join(settings.data_dir, "geo")


def ensure_seeded(settings, image_dir: str = IMAGE_ASSET_DIR) -> str:
    """Create `<data_dir>/geo` and copy the image's stock files in if they are not there yet.

    A gateway with no internet must keep routing exactly as it did before this module existed,
    so the seed is what makes the asset dir safe to point xray at on first boot.
    """
    target = geo_dir(settings)
    os.makedirs(target, exist_ok=True)
    for name in ("geoip.dat", "geosite.dat"):
        dst, src = os.path.join(target, name), os.path.join(image_dir, name)
        if not os.path.exists(dst) and os.path.exists(src):
            tmp = dst + ".seed"
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)          # never leave a half-copied dat where xray looks
    return target


def install_env(settings) -> str:
    """Point every xray this process spawns at the asset dir, once, by way of the environment.

    The supervisor, `xray -test` and the health probes all spawn xray as a child and inherit it;
    setting it here rather than at three call sites is what keeps validation and runtime reading
    the SAME files — a `-test` that passes against different data than the daemon loads is worse
    than no validation at all.
    """
    target = ensure_seeded(settings)
    os.environ[ASSET_ENV] = target
    return target


def _file_info(path: str) -> dict:
    try:
        stat = os.stat(path)
    except OSError:
        return {"present": False, "bytes": 0, "updated_at": None}
    return {"present": True, "bytes": stat.st_size, "updated_at": int(stat.st_mtime)}


def status(settings) -> list[dict]:
    """What is installed, per file: size, mtime, and whether a previous copy can be reverted to."""
    target = geo_dir(settings)
    out = []
    for dataset, files in DATASETS.items():
        for name, repo, asset in files:
            info = _file_info(os.path.join(target, name))
            out.append({"dataset": dataset, "file": name, "source": f"{repo}/{asset}",
                        "has_previous": os.path.exists(os.path.join(target, name + _SUFFIX_PREV)),
                        **info})
    return out


def _published_sha256(repo: str, asset: str, proxy: str | None) -> str | None:
    """The upstream's own checksum, or None when it publishes none for this asset."""
    try:
        body, _headers = fetch_url(_RELEASE.format(repo=repo, asset=asset + ".sha256sum"),
                                   proxy=proxy, timeout=30.0)
    except Exception as exc:
        log.info("no checksum for %s/%s: %s", repo, asset, exc)
        return None
    first = (body or "").strip().split()
    return first[0].lower() if first and len(first[0]) == 64 else None


def _download(repo: str, asset: str, dest: str, proxy: str | None) -> int:
    """Fetch one asset to `dest`, refusing an oversized body and a checksum mismatch."""
    raw, _headers = fetch_url(_RELEASE.format(repo=repo, asset=asset), proxy=proxy,
                              timeout=_DOWNLOAD_TIMEOUT, max_bytes=MAX_FILE_BYTES, raw_body=True)
    if not isinstance(raw, bytes):                       # a text body here means a wrong caller
        raise ValueError(f"{asset}: expected binary data")
    if len(raw) < 1024:
        raise ValueError(f"{asset}: {len(raw)} bytes is too small to be a geo file")
    published = _published_sha256(repo, asset, proxy)
    got = sha256(raw).hexdigest()
    if published and published != got:
        raise ValueError(f"{asset}: checksum mismatch (published {published[:12]}…, got {got[:12]}…)")
    with open(dest, "wb") as stream:
        stream.write(raw)
    return len(raw)


def _staging_dir(current: str, staged: dict[str, str]) -> tempfile.TemporaryDirectory:
    """A directory holding the files as they WOULD be: the new ones, plus links to everything
    else that is installed. `-test` has to see the whole set, not only what changed."""
    box = tempfile.TemporaryDirectory(prefix="geo-staging-")
    for name in os.listdir(current):
        if name.endswith(_SUFFIX_PREV) or name in staged:
            continue
        os.symlink(os.path.join(current, name), os.path.join(box.name, name))
    for name, path in staged.items():
        os.symlink(path, os.path.join(box.name, name))
    return box


def _live_config(settings) -> dict | None:
    import json
    try:
        with open(settings.config_path) as stream:
            cfg = json.load(stream)
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _validate_against(cfg: dict, xray_bin: str, asset_dir: str) -> tuple[bool, str]:
    previous = os.environ.get(ASSET_ENV)
    os.environ[ASSET_ENV] = asset_dir
    try:
        return validate_config(cfg, xray_bin)
    finally:
        if previous is None:
            os.environ.pop(ASSET_ENV, None)
        else:
            os.environ[ASSET_ENV] = previous


def update(state, dataset: str) -> dict:
    """Replace one dataset's files, keeping the previous copies. Returns a result dict; the only
    exceptions that escape are programming errors, never a failed download."""
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}")
    from pi_gw_panel.subs.service import tunnel_proxy
    settings = state.settings
    target = ensure_seeded(settings)
    proxy = tunnel_proxy(state)
    free = shutil.disk_usage(settings.data_dir).free
    staged: dict[str, str] = {}
    box = tempfile.TemporaryDirectory(prefix="geo-download-")
    try:
        downloaded = 0
        for name, repo, asset in DATASETS[dataset]:
            path = os.path.join(box.name, name)
            downloaded += _download(repo, asset, path, proxy)
            staged[name] = path
        # Both copies live on disk at once during the swap (new + .prev), so refuse before
        # writing rather than filling the data partition half way through.
        if free < downloaded * 3:
            raise ValueError(f"not enough free space: {free} bytes for {downloaded} of new data")

        cfg = _live_config(settings)
        if cfg is not None:
            with _staging_dir(target, staged) as staging:
                ok, detail = _validate_against(cfg, state.xray_bin or settings.xray_bin, staging)
            if not ok:
                return {"ok": False, "dataset": dataset,
                        "error": f"the running config does not load with the new {dataset} data: "
                                 f"{explain_xray_error(detail)}"}

        swapped = []
        for name, path in staged.items():
            live = os.path.join(target, name)
            if os.path.exists(live):
                os.replace(live, live + _SUFFIX_PREV)
            shutil.move(path, live)
            swapped.append(name)
    except Exception as exc:
        log.info("geo update of %s failed: %s", dataset, exc)
        return {"ok": False, "dataset": dataset, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        box.cleanup()

    reloaded = state.supervisor.reload_if_running()
    if reloaded is False:
        # `-test` passed and the daemon still did not come back. Put the old data back and say
        # so: a gateway carrying traffic is worth more than the newer list.
        _restore_previous(target, swapped)
        state.supervisor.reload_if_running()
        return {"ok": False, "dataset": dataset, "files": swapped,
                "error": "xray did not come back with the new data; the previous files were restored"}
    return {"ok": True, "dataset": dataset, "files": swapped, "reloaded": reloaded is True}


def _restore_previous(target: str, names) -> list[str]:
    restored = []
    for name in names:
        prev = os.path.join(target, name + _SUFFIX_PREV)
        if os.path.exists(prev):
            os.replace(prev, os.path.join(target, name))
            restored.append(name)
    return restored


def revert(state, dataset: str) -> dict:
    """Put the previous copies of a dataset back — the undo for an update that loads fine but
    routes worse (a category that changed meaning, a list that lost entries)."""
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}")
    target = ensure_seeded(state.settings)
    names = [name for name, _repo, _asset in DATASETS[dataset]
             if os.path.exists(os.path.join(target, name + _SUFFIX_PREV))]
    if not names:
        return {"ok": False, "dataset": dataset, "error": "there is no previous copy to go back to"}
    restored = _restore_previous(target, names)
    reloaded = state.supervisor.reload_if_running()
    return {"ok": reloaded is not False, "dataset": dataset, "files": restored,
            "reloaded": reloaded is True,
            "error": "" if reloaded is not False else "xray did not come back after the revert"}
