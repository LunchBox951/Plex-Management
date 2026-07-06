"""Health / status aggregation (ADR-0012) — "is every subsystem healthy, is the
reconcile loop running, how full is the disk" in one read.

Three independent concerns, aggregated by :func:`collect_health_snapshot`:

* **Upstream reachability** — :func:`check_subsystems` REUSES the setup wizard's
  own ``setup_validation.validate_*`` probes (the same "Test connection" checks,
  so there is exactly one definition of "is Plex/Prowlarr/qBittorrent/TMDB
  reachable"), each briefly cached (:class:`TtlCache`, ~15s) so a dashboard
  polling every few seconds never hammers an upstream or burns the TMDB rate
  limit. A subsystem with no stored credentials reports ``not_configured`` —
  honest, never a misleading ``down``.
* **Disk usage per configured root** — :func:`read_disk_usage` does the one bit
  of real I/O (``shutil.disk_usage``); :func:`collect_disk_gauges` wraps it per
  root, skipping an unset root and honestly surfacing (never crashing on) an
  unreadable one. :func:`read_disk_usage` is ALSO the eviction sweep's disk
  reading (``services/eviction_service.py``) — both features share the exact
  same byte counts and the same pure percentage math
  (:mod:`plex_manager.domain.disk_usage`), so they can never disagree about what
  "90% full" means.
* **The reconcile loop's own health** — :class:`ReconcileStatus` is a small,
  mutable, in-process record the web layer stores on ``app.state.reconcile_status``
  and mutates directly from ``_reconcile_once``/``_reconcile_loop`` (see
  ``web/app.py``); :func:`snapshot_reconcile` takes an immutable copy for a
  response. This is deliberately a SEPARATE signal from "is qBittorrent
  reachable" — the loop can complete a cycle successfully even while one
  upstream inside it is degraded (that already surfaces via its own subsystem
  card), so "is the loop itself still running" must never be conflated with "is
  every subsystem up".

This module never depends on FastAPI/Starlette or the web layer's
``SettingsStore``: callers (the eventual ``GET /api/v1/ops/health`` router)
resolve credentials and library roots themselves and pass plain values in — the
same "web layer resolves config, services take plain typed parameters" split
already used by ``web/app.py``'s reconcile loop.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from plex_manager.domain.disk_usage import DiskUsage, used_percent
from plex_manager.web.setup_validation import (
    validate_plex,
    validate_prowlarr,
    validate_qbittorrent,
    validate_tmdb,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "SUBSYSTEM_CACHE_KEYS",
    "AutograbStatus",
    "AutograbStatusSnapshot",
    "DiskGauge",
    "HealthCredentials",
    "HealthSnapshot",
    "ReconcileStatus",
    "ReconcileStatusSnapshot",
    "SubsystemHealth",
    "SubsystemState",
    "TtlCache",
    "check_database",
    "check_subsystems",
    "collect_disk_gauges",
    "collect_health_snapshot",
    "read_disk_usage",
    "snapshot_autograb",
    "snapshot_reconcile",
]


# How long a subsystem probe result stays fresh before the next poll re-hits the
# upstream (~15s per the blueprint) — short enough that "Test connection" style
# feedback stays timely, long enough that a dashboard refreshing every few
# seconds never turns into an upstream-hammering / TMDB-rate-limit-burning loop.
SUBSYSTEM_PROBE_TTL_SECONDS: float = 15.0

# The cache key per probed subsystem, in the fixed order the dashboard expects
# (and ``check_subsystems`` returns). ALSO the exact key set a caller must
# generation-snapshot BEFORE reading credentials from the database -- see
# ``TtlCache``'s invariant and ``web.routers.ops.health_endpoint``. Keep in sync
# with ``web.routers.settings._SUBSYSTEM_CREDENTIAL_FIELDS``' keys (the
# invalidation side of the same contract).
SUBSYSTEM_CACHE_KEYS: Final[tuple[str, ...]] = ("plex", "prowlarr", "qbittorrent", "tmdb")

SubsystemState = Literal["ok", "degraded", "down", "not_configured"]


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# TTL cache — generic, per-instance (NOT a module-level global): a caller (the
# eventual ops router) owns one instance per app (stored on ``app.state``), so
# separate app instances (e.g. in tests) never share stale probe results.
# --------------------------------------------------------------------------- #
class TtlCache[V]:
    """A minimal monotonic-clock TTL cache. Only fresh hits are ever returned.

    INVARIANT (Codex P2, rounds 2+3 — the "stale probe straddles an
    invalidation" race): a result may only be cached if no invalidation
    occurred after the generation snapshot that preceded the CREDENTIAL READ
    (not merely the probe's own upstream call). The full failure mode: the
    health flow reads OLD credentials from the database, a settings write
    commits and calls :meth:`invalidate`, then a probe runs with those OLD
    credentials — its ``cache.set()`` afterwards would silently resurrect the
    pre-invalidation result and serve it for another full TTL. The window
    opens the moment the credentials are read, so a snapshot taken any later
    (round 2 took it at probe start, INSIDE the ``_check_*`` helpers — after
    the endpoint's credential read) misses an invalidation that lands between
    the credential read and the snapshot, and the stale write sails through.

    Mechanics: each key carries a monotonically increasing effective
    generation (a per-key counter bumped by :meth:`invalidate`, plus a global
    epoch bumped by :meth:`clear` — a clear IS an invalidation of every key,
    including keys never seen before). The caller snapshots generations via
    :meth:`generation_snapshot`/:meth:`current_generation` STRICTLY BEFORE
    reading the credentials its probes will use, then passes the snapshot back
    into :meth:`set` as ``generation``: if the effective generation has moved
    by the time the probe completes, ``set`` silently no-ops instead of
    writing the stale value. Callers that own the only writer for a key
    (nothing can invalidate it concurrently) may omit ``generation`` and keep
    the old unconditional-write behavior -- e.g. the disk-preview cache in
    ``web/routers/ops.py``.
    """

    def __init__(self, ttl_seconds: float = SUBSYSTEM_PROBE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, V]] = {}
        # Per-key invalidation counters + the global clear() epoch. Entries are
        # never removed (a reset could collide a future effective generation
        # with an outstanding snapshot); both are bounded by the small, fixed
        # key vocabularies this cache is used with (4 subsystems / a handful of
        # library roots).
        self._generation: dict[str, int] = {}
        self._epoch = 0

    def get(self, key: str) -> V | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            del self._store[key]
            return None
        return value

    def _effective_generation(self, key: str) -> int:
        """``key``'s effective generation: its per-key :meth:`invalidate` count
        plus the global :meth:`clear` epoch. Strictly increases on every
        invalidation event that covers ``key``, never decreases -- so an
        equality check against an older snapshot detects ANY intervening
        invalidation."""
        return self._epoch + self._generation.get(key, 0)

    def current_generation(self, key: str) -> int:
        """Snapshot ``key``'s current effective generation.

        Take the snapshot STRICTLY BEFORE reading the credentials/config the
        upcoming probe will use -- not merely before the probe's own upstream
        call -- then pass it back into :meth:`set` once the probe completes.
        See the class docstring's invariant for why the credential read, not
        the probe, opens the race window.
        """
        return self._effective_generation(key)

    def generation_snapshot(self, keys: Iterable[str]) -> dict[str, int]:
        """Snapshot :meth:`current_generation` for each of ``keys`` in one call.

        The multi-key form the health endpoint uses with
        :data:`SUBSYSTEM_CACHE_KEYS` at the very top of its flow, BEFORE its
        ``SettingsStore`` credential reads; the dict then threads through
        :func:`check_subsystems` into each probe helper's ``cache.set()``.
        """
        return {key: self.current_generation(key) for key in keys}

    def set(self, key: str, value: V, *, generation: int | None = None) -> None:
        """Cache ``value`` under ``key``.

        ``generation``, when given, must be a snapshot
        :meth:`current_generation`/:meth:`generation_snapshot` took for this
        key BEFORE the credentials feeding the probe were read (see the class
        docstring). If :meth:`invalidate` (this key) or :meth:`clear` ran
        since then, the effective generation has moved and this call is a
        silent no-op -- writing the stale result back in would resurrect
        pre-invalidation state for another full TTL. Omit ``generation`` to
        always write unconditionally.
        """
        if generation is not None and generation != self._effective_generation(key):
            return
        self._store[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        """Drop every cached entry and advance every key's effective generation.

        A test-isolation helper AND the production invalidation hook
        ``POST /evict`` calls on the disk-preview cache after a sweep — see
        ``web.routers.ops.evict_endpoint`` — so a stale pre-eviction snapshot
        is never served back to the operator who just triggered the sweep.

        Bumps the global epoch rather than per-key counters: a clear is an
        invalidation of EVERY key, including keys this instance has never seen
        (an in-flight probe for a brand-new key still snapshotted the epoch),
        so the class invariant holds for generation-guarded writers under
        ``clear()`` exactly as under :meth:`invalidate`. Unconditional
        (generation-less) ``set()`` callers are unaffected.
        """
        self._store.clear()
        self._epoch += 1

    def invalidate(self, key: str) -> None:
        """Drop ONE cached entry (a no-op if it isn't present) and bump its
        generation counter.

        The targeted counterpart to :meth:`clear` (issue #93): ``PUT /settings``
        calls this per AFFECTED subsystem after a successful credential save —
        e.g. a Plex URL/token edit invalidates only the ``"plex"`` entry — so the
        very next ``GET /health`` re-probes that subsystem instead of serving up
        to ``SUBSYSTEM_PROBE_TTL_SECONDS`` of pre-edit ``ok``/``down``/
        ``not_configured`` state back to the operator who just fixed (or broke) a
        credential. Deliberately narrower than :meth:`clear`: an edit to ONE
        subsystem's credentials must never discard another subsystem's still-valid
        cached probe.

        The generation bump ALWAYS happens, even when there was nothing cached to
        drop: a health request may already hold this key's generation snapshot
        (taken before its credential read) without having cached anything yet, so
        bumping unconditionally still fences out its eventual stale ``set()``
        (see the class docstring's invariant).
        """
        self._store.pop(key, None)
        self._generation[key] = self._generation.get(key, 0) + 1


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SubsystemHealth:
    """One subsystem's reachability, as the health dashboard renders a card."""

    name: str
    status: SubsystemState
    detail: str | None
    checked_at: datetime


@dataclass(frozen=True)
class DiskGauge:
    """One configured library root's usage snapshot.

    ``error`` is set (and ``total_bytes``/``available_bytes``/``used_percent``
    are ``0``) when the root's filesystem could not be read (missing mount,
    permission denied, ...) — an honest failure, never a crash of the whole
    health snapshot.
    """

    root: str
    path: str
    total_bytes: int
    available_bytes: int
    used_percent: float
    error: str | None = None


@dataclass
class ReconcileStatus:
    """Mutable, in-process record of the BACKGROUND RECONCILE LOOP's own health
    (ADR-0012) — never persisted, lost on restart (a fresh process legitimately
    has no history yet).

    Deliberately separate from :class:`SubsystemHealth`: a cycle can complete
    successfully (``mark_ok``) even while one upstream INSIDE it degraded (e.g. a
    qBittorrent outage the cycle already tolerates and logs — see
    ``web/app.py``'s ``_reconcile_once``) — that is reported via its OWN
    subsystem card, never conflated with "is the loop itself still running".

    Mutated in place (never replaced) so the single instance the web layer stores
    on ``app.state.reconcile_status`` stays the same object every cycle mutates —
    exactly like ``app.state.sessionmaker``/``http_client``.
    """

    last_run_at: datetime | None = field(default=None)
    last_ok_at: datetime | None = field(default=None)
    last_error_type: str | None = field(default=None)
    last_error_at: datetime | None = field(default=None)
    consecutive_failures: int = field(default=0)

    def mark_run_started(self) -> None:
        """Stamp the top of a new cycle — called unconditionally, success or not."""
        self.last_run_at = _now()

    def mark_ok(self) -> None:
        """A cycle completed without raising: clear any prior error, reset the streak."""
        self.last_ok_at = _now()
        self.last_error_type = None
        self.last_error_at = None
        self.consecutive_failures = 0

    def mark_error(self, exc: BaseException) -> None:
        """A cycle raised: record the exception TYPE only (never its message — no
        secret leak) and extend the consecutive-failure streak."""
        self.last_error_type = type(exc).__name__
        self.last_error_at = _now()
        self.consecutive_failures += 1


@dataclass(frozen=True)
class ReconcileStatusSnapshot:
    """An immutable copy of :class:`ReconcileStatus`, safe to hand to a response
    without exposing the mutable original."""

    last_run_at: datetime | None
    last_ok_at: datetime | None
    last_error_type: str | None
    last_error_at: datetime | None
    consecutive_failures: int


def snapshot_reconcile(status: ReconcileStatus) -> ReconcileStatusSnapshot:
    """Take an immutable point-in-time copy of the live, mutable ``status``."""
    return ReconcileStatusSnapshot(
        last_run_at=status.last_run_at,
        last_ok_at=status.last_ok_at,
        last_error_type=status.last_error_type,
        last_error_at=status.last_error_at,
        consecutive_failures=status.consecutive_failures,
    )


@dataclass
class AutograbStatus:
    """Mutable, in-process record of the BACKGROUND AUTO-GRAB LOOP's own health
    (ADR-0013) -- the exact mirror of :class:`ReconcileStatus`, for the separate
    ``_autograb_loop`` in ``web/app.py``.

    Deliberately a SEPARATE signal from both the subsystem cards and the reconcile
    loop: the auto-grab loop can complete a cycle cleanly (``mark_ok``) while
    Prowlarr is degraded -- and, conversely, a Prowlarr outage surfaces here as a
    failing loop (``mark_error``) so the operator sees WHY nothing is being grabbed,
    not just that requests sit at ``pending``. Never persisted (a fresh process
    legitimately has no history yet); mutated in place like ``ReconcileStatus``.
    """

    last_run_at: datetime | None = field(default=None)
    last_ok_at: datetime | None = field(default=None)
    last_error_type: str | None = field(default=None)
    last_error_at: datetime | None = field(default=None)
    consecutive_failures: int = field(default=0)
    # How many scopes are CURRENTLY inside a grab-pipeline cooldown (ADR-0013):
    # scopes whose grab keeps raising ``GrabError`` and are being skipped so they
    # don't starve the per-cycle search budget. Surfaced so the operator SEES the
    # grab pipeline failing (honesty over silence), not just eager requests that
    # never reach ``downloading``. Set from each cycle's ``AutograbCycleResult``;
    # orthogonal to the error streak, so ``mark_ok``/``mark_error`` leave it alone.
    cooled_down_scopes: int = field(default=0)

    def mark_run_started(self) -> None:
        """Stamp the top of a new cycle -- called unconditionally, success or not."""
        self.last_run_at = _now()

    def mark_ok(self) -> None:
        """A cycle completed without raising: clear any prior error, reset the streak."""
        self.last_ok_at = _now()
        self.last_error_type = None
        self.last_error_at = None
        self.consecutive_failures = 0

    def mark_error(self, exc: BaseException) -> None:
        """A cycle raised: record the exception TYPE only (never its message -- no
        secret leak) and extend the consecutive-failure streak."""
        self.last_error_type = type(exc).__name__
        self.last_error_at = _now()
        self.consecutive_failures += 1


@dataclass(frozen=True)
class AutograbStatusSnapshot:
    """An immutable copy of :class:`AutograbStatus`, safe to hand to a response."""

    last_run_at: datetime | None
    last_ok_at: datetime | None
    last_error_type: str | None
    last_error_at: datetime | None
    consecutive_failures: int
    cooled_down_scopes: int


def snapshot_autograb(status: AutograbStatus) -> AutograbStatusSnapshot:
    """Take an immutable point-in-time copy of the live, mutable ``status``."""
    return AutograbStatusSnapshot(
        last_run_at=status.last_run_at,
        last_ok_at=status.last_ok_at,
        last_error_type=status.last_error_type,
        last_error_at=status.last_error_at,
        consecutive_failures=status.consecutive_failures,
        cooled_down_scopes=status.cooled_down_scopes,
    )


@dataclass(frozen=True)
class HealthCredentials:
    """Every credential :func:`check_subsystems` might need, all optional.

    Resolved by the caller (the ops router, via ``SettingsStore`` — see the
    module docstring on why this module never reads settings itself) so an
    unconfigured service is simply a ``None``/empty field here, never a lookup
    this module performs on its own.
    """

    plex_url: str | None = None
    plex_token: str | None = None
    prowlarr_url: str | None = None
    prowlarr_api_key: str | None = None
    qbittorrent_url: str | None = None
    qbittorrent_username: str | None = None
    qbittorrent_password: str | None = None
    tmdb_api_key: str | None = None


@dataclass(frozen=True)
class HealthSnapshot:
    """The full aggregate :func:`collect_health_snapshot` returns."""

    subsystems: tuple[SubsystemHealth, ...]
    disks: tuple[DiskGauge, ...]
    reconcile: ReconcileStatusSnapshot
    autograb: AutograbStatusSnapshot


# --------------------------------------------------------------------------- #
# Subsystem reachability (cached upstream probes)
# --------------------------------------------------------------------------- #
async def _not_configured(name: str) -> SubsystemHealth:
    return SubsystemHealth(name=name, status="not_configured", detail=None, checked_at=_now())


async def _check_plex(
    client: httpx.AsyncClient,
    creds: HealthCredentials,
    cache: TtlCache[SubsystemHealth],
    generation: int,
) -> SubsystemHealth:
    # ``generation`` is the caller's snapshot, taken BEFORE ``creds`` was read
    # (round 3: snapshotting here, after the credential read, misses an
    # invalidation landing in between) -- see ``TtlCache``'s invariant and
    # ``check_subsystems``.
    cached = cache.get("plex")
    if cached is not None:
        return cached
    if not creds.plex_url or not creds.plex_token:
        result = await _not_configured("plex")
    else:
        response = await validate_plex(client, creds.plex_url, creds.plex_token)
        result = SubsystemHealth(
            name="plex",
            status="ok" if response.ok else "down",
            detail=None if response.ok else response.message,
            checked_at=_now(),
        )
    cache.set("plex", result, generation=generation)
    return result


async def _check_prowlarr(
    client: httpx.AsyncClient,
    creds: HealthCredentials,
    cache: TtlCache[SubsystemHealth],
    generation: int,
) -> SubsystemHealth:
    # ``generation``: the caller's pre-credential-read snapshot -- see ``_check_plex``.
    cached = cache.get("prowlarr")
    if cached is not None:
        return cached
    if not creds.prowlarr_url or not creds.prowlarr_api_key:
        result = await _not_configured("prowlarr")
    else:
        response = await validate_prowlarr(client, creds.prowlarr_url, creds.prowlarr_api_key)
        result = SubsystemHealth(
            name="prowlarr",
            status="ok" if response.ok else "down",
            detail=None if response.ok else response.message,
            checked_at=_now(),
        )
    cache.set("prowlarr", result, generation=generation)
    return result


async def _check_qbittorrent(
    client: httpx.AsyncClient,
    creds: HealthCredentials,
    cache: TtlCache[SubsystemHealth],
    generation: int,
) -> SubsystemHealth:
    # ``generation``: the caller's pre-credential-read snapshot -- see ``_check_plex``.
    cached = cache.get("qbittorrent")
    if cached is not None:
        return cached
    if (
        not creds.qbittorrent_url
        or not creds.qbittorrent_username
        or creds.qbittorrent_password is None
    ):
        result = await _not_configured("qbittorrent")
    else:
        response = await validate_qbittorrent(
            client, creds.qbittorrent_url, creds.qbittorrent_username, creds.qbittorrent_password
        )
        result = SubsystemHealth(
            name="qbittorrent",
            status="ok" if response.ok else "down",
            detail=None if response.ok else response.message,
            checked_at=_now(),
        )
    cache.set("qbittorrent", result, generation=generation)
    return result


async def _check_tmdb(
    client: httpx.AsyncClient,
    creds: HealthCredentials,
    cache: TtlCache[SubsystemHealth],
    generation: int,
) -> SubsystemHealth:
    # ``generation``: the caller's pre-credential-read snapshot -- see ``_check_plex``.
    cached = cache.get("tmdb")
    if cached is not None:
        return cached
    if not creds.tmdb_api_key:
        result = await _not_configured("tmdb")
    else:
        response = await validate_tmdb(client, creds.tmdb_api_key)
        result = SubsystemHealth(
            name="tmdb",
            status="ok" if response.ok else "down",
            detail=None if response.ok else response.message,
            checked_at=_now(),
        )
    cache.set("tmdb", result, generation=generation)
    return result


async def check_subsystems(
    client: httpx.AsyncClient,
    creds: HealthCredentials,
    cache: TtlCache[SubsystemHealth],
    generations: Mapping[str, int] | None = None,
) -> list[SubsystemHealth]:
    """Return plex/prowlarr/qbittorrent/tmdb reachability, each TTL-cached.

    Reuses ``setup_validation.validate_*`` (the exact "Test connection" probes) so
    there is one definition of "is this upstream reachable" for the whole app.

    ``generations`` is the caller's :meth:`TtlCache.generation_snapshot` over
    :data:`SUBSYSTEM_CACHE_KEYS` (a ``KeyError`` on a missing key is an honest
    programming error, never silently patched over). A caller that read
    ``creds`` from a store that concurrent invalidations guard — the health
    endpoint's ``SettingsStore`` reads — MUST take that snapshot strictly
    BEFORE the read and pass it here (Codex round 3): snapshotting any later
    (e.g. at probe start, as round 2 did inside the ``_check_*`` helpers)
    misses an invalidation landing between the credential read and the
    snapshot, so the probe's stale, pre-invalidation result would be cached
    for another full TTL. ``None`` (the default) snapshots at entry, which is
    sound ONLY when ``creds`` did not come from such a store — e.g. tests
    passing literal credentials — because then no invalidation-guarded read
    precedes this call.

    The four probes are independent (each keyed to its OWN cache entry — see
    ``_check_plex``/``_check_prowlarr``/``_check_qbittorrent``/``_check_tmdb``, no
    shared mutable state between them) and each carries the app's own ~30s httpx
    timeout, so running them sequentially would serialize worst-case wait times
    into minutes whenever several upstreams are simultaneously blackholed (a
    timeout, not a fast connection-refused) — stalling the Status page exactly
    during an outage, the one moment it matters most. ``asyncio.gather`` runs
    them concurrently so the wall-clock cost is the SLOWEST probe, not the sum,
    while still returning them in the fixed plex/prowlarr/qbittorrent/tmdb order
    the dashboard expects. Each probe helper already converts a failure into a
    ``down``/``not_configured`` result rather than raising, so ``gather`` never
    needs (and must never gain) a blanket exception handler here.
    """
    if generations is None:
        generations = cache.generation_snapshot(SUBSYSTEM_CACHE_KEYS)
    plex, prowlarr, qbittorrent, tmdb = await asyncio.gather(
        _check_plex(client, creds, cache, generations["plex"]),
        _check_prowlarr(client, creds, cache, generations["prowlarr"]),
        _check_qbittorrent(client, creds, cache, generations["qbittorrent"]),
        _check_tmdb(client, creds, cache, generations["tmdb"]),
    )
    return [plex, prowlarr, qbittorrent, tmdb]


# --------------------------------------------------------------------------- #
# DB ping — cheap and local, always fresh (never TTL-cached)
# --------------------------------------------------------------------------- #
async def check_database(session: AsyncSession) -> SubsystemHealth:
    """``SELECT 1`` — the DB subsystem card. A failure here means the app itself
    is in serious trouble, so it is caught (never left to crash the whole
    snapshot) and surfaced as ``down`` with the exception type only."""
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return SubsystemHealth(
            name="database", status="down", detail=type(exc).__name__, checked_at=_now()
        )
    return SubsystemHealth(name="database", status="ok", detail=None, checked_at=_now())


# --------------------------------------------------------------------------- #
# Disk usage — the one bit of real I/O, shared with eviction_service
# --------------------------------------------------------------------------- #
def read_disk_usage(path: str) -> DiskUsage:
    """Read total/free bytes for the filesystem containing ``path``.

    A single ``shutil.disk_usage()`` call (not two) so the total/free pair is
    read atomically from the same snapshot. Raises ``OSError`` if ``path`` is
    missing/unreadable/unmounted — the caller decides how to surface that
    honestly: :func:`collect_disk_gauges` wraps it into a :class:`DiskGauge`
    with ``error`` set; ``eviction_service`` treats an unreadable root as "skip
    this root's sweep this tick, log a warning" (never a crash).

    Shared verbatim by the health dashboard's per-root gauge and the eviction
    pressure check (:mod:`plex_manager.services.eviction_service`) — see the
    module docstring on why the two features must read the SAME number.

    Synchronous (a plain ``statvfs`` syscall under the hood) — a hung/
    unresponsive NFS/SMB mount can stall this call indefinitely, so every
    ``async def`` caller MUST run it via ``await asyncio.to_thread(...)``
    rather than inline, or it would freeze the whole event loop, not just its
    own request. :func:`collect_disk_gauges` (below) is itself plain/sync for
    the same reason :func:`_size_bytes` in ``eviction_service`` is: it is the
    caller's job to offload the whole thing in one hop (mirrors
    ``import_service``'s ``asyncio.to_thread``-wrapped copy) rather than
    threading each individual root one at a time.
    """
    usage = shutil.disk_usage(path)
    return DiskUsage(root=path, total_bytes=usage.total, available_bytes=usage.free)


def collect_disk_gauges(roots: dict[str, str | None]) -> list[DiskGauge]:
    """Build a :class:`DiskGauge` per CONFIGURED root in ``roots`` (label -> path).

    An unset root (``None``/empty path) is skipped honestly — there is nothing to
    gauge. A configured-but-unreadable root is NOT skipped: it is reported with
    ``error`` set and zeroed byte counts, so a broken mount is visible on the
    dashboard rather than silently vanishing from it.

    Deliberately plain ``def`` (not ``async``): it may call :func:`read_disk_usage`
    (blocking) once per configured root. :func:`collect_health_snapshot` is the
    ONLY caller and runs the whole thing via ``await asyncio.to_thread(...)`` —
    see that function — so a stalled mount blocks a worker thread, never the
    event loop.
    """
    gauges: list[DiskGauge] = []
    for label, path in roots.items():
        if not path:
            continue
        try:
            usage = read_disk_usage(path)
        except OSError as exc:
            gauges.append(
                DiskGauge(
                    root=label,
                    path=path,
                    total_bytes=0,
                    available_bytes=0,
                    used_percent=0.0,
                    error=str(exc),
                )
            )
            continue
        gauges.append(
            DiskGauge(
                root=label,
                path=path,
                total_bytes=usage.total_bytes,
                available_bytes=usage.available_bytes,
                used_percent=used_percent(usage),
                error=None,
            )
        )
    return gauges


# --------------------------------------------------------------------------- #
# The aggregate
# --------------------------------------------------------------------------- #
async def collect_health_snapshot(
    *,
    session: AsyncSession,
    client: httpx.AsyncClient,
    cache: TtlCache[SubsystemHealth],
    creds: HealthCredentials,
    reconcile_status: ReconcileStatus,
    autograb_status: AutograbStatus,
    library_roots: dict[str, str | None],
    generations: Mapping[str, int] | None = None,
) -> HealthSnapshot:
    """Aggregate every health signal into one snapshot for the dashboard.

    ``library_roots`` is a label -> path mapping (e.g.
    ``{"movies_root": ..., "tv_root": ...}``); see :func:`collect_disk_gauges`.

    ``generations`` passes straight through to :func:`check_subsystems` (see
    its docstring): a caller that resolved ``creds`` from the settings store
    MUST supply the :meth:`TtlCache.generation_snapshot` it took BEFORE that
    read, or a settings save landing mid-request can have its invalidation
    silently overwritten by a probe still running on the old credentials.

    ``collect_disk_gauges`` is run via ``asyncio.to_thread`` -- it calls the
    blocking ``shutil.disk_usage`` once per configured root, and a hung/
    unresponsive NFS/SMB mount must never freeze this (or any other request's)
    event-loop turn while a health poll waits on it.
    """
    subsystems = await check_subsystems(client, creds, cache, generations)
    subsystems.append(await check_database(session))
    disks = await asyncio.to_thread(collect_disk_gauges, library_roots)
    return HealthSnapshot(
        subsystems=tuple(subsystems),
        disks=tuple(disks),
        reconcile=snapshot_reconcile(reconcile_status),
        autograb=snapshot_autograb(autograb_status),
    )
