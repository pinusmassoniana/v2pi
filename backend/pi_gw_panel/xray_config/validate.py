import errno
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pi_gw_panel.config import Settings

logger = logging.getLogger(__name__)

# xray -test can echo config content on error; these keys carry credentials/secrets that must
# never reach logs or an API response (the VLESS uuid is effectively the exit-node password).
# Plural spellings matter: the road-warrior Reality inbound emits `shortIds` as a LIST.
_SECRET_KEYS = {
    "uuid", "id", "password", "publickey", "privatekey", "shortid", "shortids",
    "public_key", "private_key", "short_id", "short_ids",
}
_VALIDATE_TIMEOUT = 15.0


def _add_secret(value, out: set[str]) -> None:
    """Collect every string reachable under a secret key.

    A secret value is not always a bare string: Reality's `shortIds` is a list of hex ids, and
    a list-valued secret used to fall through to the plain recursion below, which only ever
    looks at dict keys — so every road-warrior short id stayed in the clear."""
    if isinstance(value, str):
        if value:
            out.add(value)
    elif isinstance(value, list):
        for item in value:
            _add_secret(item, out)


def _collect_secrets(obj, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in _SECRET_KEYS:
                _add_secret(v, out)
            if isinstance(v, (dict, list)):
                _collect_secrets(v, out)   # keep descending: secrets nest under secret keys too
    elif isinstance(obj, list):
        for v in obj:
            _collect_secrets(v, out)


def scrub_output(text: str, cfg: dict) -> str:
    secrets: set[str] = set()
    _collect_secrets(cfg, secrets)
    # Longest first: a short id can be a prefix of a longer secret, and replacing the short one
    # first would leave a partially-redacted remainder behind.
    for s in sorted(secrets, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def validate_config(cfg: dict, xray_bin: str) -> tuple[bool, str]:
    """Run `xray -test -config <tmp>`; return (ok, combined-output with secrets scrubbed)."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        try:
            proc = subprocess.run(
                [xray_bin, "-test", "-config", tmp],
                capture_output=True, text=True, timeout=_VALIDATE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # a hung -test would otherwise wedge the whole apply path (it runs synchronously
            # before any write/restart); the child is already killed by run() on timeout.
            return False, "validation timed out"
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                return False, "xray executable not found"
            return False, f"unable to run xray validation: {exc.strerror or type(exc).__name__}"
        return proc.returncode == 0, scrub_output((proc.stdout + proc.stderr).strip(), cfg)
    finally:
        os.unlink(tmp)


def config_digest(cfg: dict) -> str:
    """Content digest of a config, stable under reformatting (key order, indentation).

    Used to pair the last-good snapshot with the apply that produced it; hashing the file bytes
    instead would make the pairing depend on how json.dump happened to lay the file out."""
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Errors that mean "this filesystem has no directory fsync", as opposed to "the fsync failed".
# Treated as durable-as-it-gets: refusing every revocation on such a filesystem would turn a
# durability nicety into a permanent inability to cut a lost device off.
_FSYNC_UNSUPPORTED = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}


def _fsync_dir(path: str) -> bool:
    """Make a completed rename durable; return whether it is now provably on stable storage.

    Callers that only want the nicety ignore the result — some filesystems refuse O_RDONLY fsync
    on a directory, and a failure here must never fail an otherwise-good apply. Callers whose
    correctness depends on the change surviving a power cut (see `_invalidate_provenance_durably`)
    check it.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        logger.debug("could not open %s to fsync: %s", path, exc)
        return exc.errno in _FSYNC_UNSUPPORTED
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("could not fsync directory %s: %s", path, exc)
        return exc.errno in _FSYNC_UNSUPPORTED
    finally:
        os.close(fd)
    return True


class ConfigManager:
    """Owns the on-disk live config + last-good snapshot + rollback."""

    def __init__(self, settings: Settings, xray_bin: str | None = None):
        self.settings = settings
        self.xray_bin = xray_bin or settings.xray_bin

    @property
    def _provenance_path(self) -> str:
        """Sidecar recording WHICH apply produced the current last-good snapshot.

        Derived from lastgood_path so it always lives beside it (same 0700 data dir, same
        owner-only mode via _write_atomic) and needs no new Settings field."""
        return self.settings.lastgood_path + ".provenance"

    def apply(self, cfg: dict) -> tuple[bool, str]:
        ok, out = validate_config(cfg, self.xray_bin)
        if not ok:
            return False, out
        # Snapshot the currently-live config as the rollback (undo) target, THEN write the
        # new one — so rollback() reverts to the *previous* apply, not the one just made.
        # The snapshot is a convenience, never a precondition: an unreadable live config
        # (truncated by a power cut, hand-edited over SSH) must not be able to fail EVERY
        # future apply — that turns the one screen that could repair it into a dead end.
        # The existing last-good file survives as a repair artifact (it is intact, unlike the
        # file we just failed to read), but it stops being a rollback TARGET: see below.
        previous = None
        snapshotted = False
        if os.path.exists(self.settings.config_path):
            try:
                with open(self.settings.config_path) as f:
                    previous = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "could not snapshot the live xray config as the rollback target "
                    "(%s) — applying anyway; the older last-good file is kept but is no "
                    "longer a valid rollback target", exc)
            else:
                snapshotted = True
        if snapshotted:
            self._write_atomic(self.settings.lastgood_path, previous)
            # Pair the snapshot with this apply: the retained file IS what `cfg` replaces.
            self._write_atomic(self._provenance_path,
                               {"lastgood": config_digest(previous), "live": config_digest(cfg)})
        else:
            # Keeping the older last-good is right (it is a repair artifact), CALLING it the
            # rollback target is not: it is whatever some earlier apply left behind, unrelated
            # to the config being replaced now. Callers roll back believing the result restores
            # the node they were just on — promoting a stale file there can resurrect revoked
            # road-warrior clients or routing rules a later apply had fixed. Provenance is
            # dropped for this attempt; the next apply that can read the live config restores it.
            self._invalidate_provenance()
        self._write_atomic(self.settings.config_path, cfg)
        return True, out

    def apply_irreversible(self, cfg: dict) -> tuple[bool, str]:
        """`apply()` for a config whose effect a rollback may NEVER undo — a revocation.

        Same validate-then-atomically-replace as `apply()`, and deliberately different in the two
        ways that matter:

          * It never publishes a rollback target. `apply()` files the config it REPLACES as the
            undo, so revoking through it necessarily snapshots the credential-bearing config and
            marks the pairing valid — `POST /rollback` then writes the revoked client back and,
            with a previous node on file, reloads (which on a stopped xray is a start). Doing
            that and then unlinking the marker afterwards leaves the door open for exactly as
            long as the unlink can fail: after a successful revocation the marker matches the
            clean live config and the pre-revocation snapshot EXACTLY, so a swallowed error there
            is a promotable pre-revocation target, not the harmless stale pairing the ordinary
            path can tolerate. So the pairing is never written in the first place.
          * The invalidation comes FIRST and has to be durable. Ordering is the whole crash
            argument: invalidate-then-write means the only states a power cut can leave are
            "old config, no promotable target" (the revocation simply did not happen — the start
            path re-sanitizes) and "new config, no promotable target" (it did). Write-then-
            invalidate has a window that leaves "new config, promotable PRE-revocation target",
            which is the leak with the operator already told the device was cut off.

        Fails closed: a provenance marker that cannot be durably invalidated aborts the apply
        BEFORE the live config is touched, so the caller sees a failed revocation (and stops
        xray) rather than a completed one that a rollback can quietly reverse.

        The last-good file is left exactly as it is: still a repair artifact, no longer a
        promotable target, and never rewritten to hold the credential just revoked.
        """
        ok, out = validate_config(cfg, self.xray_bin)
        if not ok:
            return False, out
        if not self._invalidate_provenance_durably():
            return False, ("refusing to apply: the rollback provenance marker could not be "
                           "durably invalidated, and this config must not be reversible")
        self._write_atomic(self.settings.config_path, cfg)
        return True, out

    def rollback_target(self, *, log: bool = False) -> dict | None:
        """The config `rollback()` would install, or None when there is no PROVABLE one.

        Provable means: the snapshot on disk is intact, and it is the config that the config
        currently live was applied over — both checked by content digest against the marker
        `apply()` writes. Anything else — a missing or damaged marker, a live config replaced
        out of band, an upgrade from before the marker existed — fails closed.

        `log` asks for a refusal to be reported at WARNING, and is off by default because the
        common caller is `rollback_available()` — which `/api/status` polls every few seconds.
        After a revocation the marker is invalidated deliberately and permanently, so warning
        on the CHECK turned an ordinary, correct revocation into a warning every poll for as
        long as the panel ran, burying the entries an operator actually needs. An expected
        state is not a warning. A refusal only becomes news when somebody tried to roll back,
        so `rollback()` asks for it and every other caller gets the same text at DEBUG.
        """
        report = logger.warning if log else logger.debug
        path = self.settings.lastgood_path
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                lastgood = json.load(f)  # refuse to promote a corrupt/partial snapshot
        except (OSError, json.JSONDecodeError):
            return None
        try:
            with open(self._provenance_path) as f:
                marker = json.load(f)
            with open(self.settings.config_path) as f:
                live = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            report("refusing to roll back: rollback provenance unreadable (%s)", exc)
            return None
        if isinstance(marker, dict) and marker.get("invalidated"):
            report(
                "refusing to roll back: the rollback target was deliberately invalidated "
                "(an access revocation is not undoable); the next apply restores the ability")
            return None
        if (not isinstance(marker, dict)
                or marker.get("lastgood") != config_digest(lastgood)
                or marker.get("live") != config_digest(live)):
            report(
                "refusing to roll back: the last-good snapshot is not the config that the live "
                "one replaced — restoring it would install an unrelated older config")
            return None
        return lastgood

    def invalidate_rollback(self) -> bool:
        """Make `rollback()` fail closed until some later apply pairs a fresh snapshot with a
        fresh live config, and report whether that is now PROVABLY the case. The snapshot FILE is
        kept (it is still a repair artifact), it simply stops being a promotable target — exactly
        what a failed snapshot already does.

        Returns False when the marker could not be neutralised. That answer is load-bearing: for
        a revocation that reached the live config through `apply()` (the reapply path) the marker
        left behind matches the clean live config and the pre-revocation snapshot exactly, so a
        failure here is a promotable pre-revocation target, not a stale pairing that the digest
        check would reject anyway. Prefer `apply_irreversible`, which never creates that pairing;
        this is the sweep for the paths that wrote through something else, or wrote nothing.

        For actions whose effect a rollback must never undo. A revocation is the whole of that
        list: apply() snapshots the config it replaces, so revoking necessarily files the
        credential-bearing config as the rollback target, and `POST /rollback` would then write
        the revoked client back AND (with a previous node on file) reload — starting an xray the
        revocation had stopped. One button, both halves of the leak, minutes after the operator
        was told the lost device was cut off.

        Deliberately the same mechanism as the provenance pairing rather than a second one:
        rollback() already fails closed on an unreadable marker, so there is nothing new to get
        wrong, no new state to keep in sync, and the ability comes back by itself on the next
        apply that can be vouched for. Until then `POST /rollback` answers `{"ok": false}` —
        the same answer it has always given when there is no provable target — and
        `rollback_available()` says so without side effects.
        """
        return self._invalidate_provenance_durably()

    def rollback_available(self) -> bool:
        """Read-only: would rollback() succeed? (No side effects and no log noise — safe for a
        status view polled every few seconds; see `rollback_target`'s `log`.)"""
        return self.rollback_target() is not None

    def rollback(self) -> bool:
        lastgood = self.rollback_target(log=True)   # someone asked: a refusal needs its reason
        if lastgood is None:
            return False
        self._write_atomic(self.settings.config_path, lastgood)
        # The snapshot is now also the live config, so a repeated rollback stays a provable
        # no-op instead of becoming an unprovable promotion.
        digest = config_digest(lastgood)
        self._write_atomic(self._provenance_path, {"lastgood": digest, "live": digest})
        return True

    def _invalidate_provenance(self) -> None:
        try:
            os.unlink(self._provenance_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Not fatal: the marker still records the digest of an OLDER live config, so the
            # pairing check in rollback_target() rejects it anyway. Worth a line in the log.
            logger.warning("could not drop the stale rollback provenance marker: %s", exc)

    def _invalidate_provenance_durably(self) -> bool:
        """Neutralise the provenance marker so `rollback_target()` fails closed, and do it in a
        way that survives a power cut. Returns whether it provably worked.

        REPLACES the marker with `{"invalidated": true}` rather than unlinking it. Both make
        rollback fail closed, but only a write goes through `_write_atomic` — fsync the contents,
        rename, fsync the directory — so the change is on stable storage when this returns. An
        unlink's durability rests entirely on that same directory fsync, which the ordinary path
        does best-effort and ignores; an unlink lost to a power cut brings the old marker back,
        and after a revocation that marker is a valid pairing of the pre-revocation snapshot with
        a live config that no longer grants what it grants. A file that says why it is invalid is
        also what an operator finds beside the snapshot when they wonder why Rollback is greyed
        out.

        Self-healing exactly as before: the next `apply()` that can read the live config
        overwrites this with a real pairing.

        Never raises. One caller runs this in a `finally` after a revocation, where an exception
        would replace the revocation's own outcome with this one — the operator would be told
        the marker failed and never learn whether the device was actually cut off. A failure is
        reported as False, which the caller can act on.
        """
        try:
            durable = self._write_atomic(self._provenance_path, {"invalidated": True})
        except Exception as exc:
            logger.error("could not invalidate the rollback provenance marker: %s", exc)
            return False
        if not durable:
            logger.error("could not fsync the directory holding the rollback provenance marker; "
                         "its invalidation is not proven durable")
        return durable

    @staticmethod
    def _write_atomic(path: str, cfg: dict) -> bool:
        """Write cfg as JSON via temp file + os.replace (atomic on POSIX).

        `os.replace` is only atomic w.r.t. the *rename*: without an fsync the renamed name can
        still resolve to unwritten data after a power cut, leaving a zero-length config.json or
        lastgood. xray then refuses to start at boot and the panel cannot repair itself. So:
        fsync the contents before the rename, then fsync the directory to make the rename itself
        durable (same order dnsmasq_supervisor uses for its conf).

        Returns whether the rename is provably durable. Callers for whom durability is only a
        nicety ignore it; `_invalidate_provenance_durably` does not."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=parent or None)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return _fsync_dir(parent or ".")
