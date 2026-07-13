"""Shared upper bounds for the typed operability settings (issue #92).

One source of truth for "how high can this setting go", used by BOTH the
write-time ``Field(le=...)`` constraints on ``SettingsUpdate``
(``web.schemas``) and the read-time range guards on the typed getters
(``web.deps``) -- so a value that is rejected on the way in is the exact same
value a corrupt/hand-edited stored row is range-guarded against on the way
out. No drift between the two is possible because there is only one set of
numbers.

Import hygiene note: ``web.deps`` transitively imports ``web.schemas`` (via
``services.health_service`` -> ``web.setup_validation`` -> ``web.schemas``),
so ``web.schemas`` must NOT import ``web.deps`` directly -- doing so is a
verified circular import (``ImportError: cannot import name ... from
partially initialized module``). This module has no project imports of its
own, so both sides can depend on it with no ordering hazard, mirroring
``url_validation.py``'s identical "shared, dependency-free leaf" role.
"""

from __future__ import annotations

__all__ = [
    "DISK_PRESSURE_PERCENT_MAX",
    "DISK_PRESSURE_PERCENT_MIN",
    "EVICTION_GRACE_DAYS_MAX",
    "EVICTION_INTERVAL_MAX_MINUTES",
    "LOG_MAX_ROWS_MAX",
    "LOG_RETENTION_DAYS_MAX",
]

# The disk-pressure trigger threshold and target are a USED-DISK PERCENT, so both
# sit on the closed ``[0, 100]`` scale. Shared here so the write-time
# ``Field(ge=..., le=...)`` on ``SettingsUpdate`` (``web.schemas``) and the
# read-time range guard on ``get_disk_pressure_threshold_percent`` /
# ``get_disk_pressure_target_percent`` (``web.deps``) reject the EXACT same
# out-of-range value -- a stored ``150`` / ``-1`` that ``PUT`` refuses is the same
# value the runtime getter degrades to its default, so ``GET /settings`` (which
# nulls it) and the eviction sweep can never disagree on the effective percentage.
DISK_PRESSURE_PERCENT_MIN: float = 0.0
DISK_PRESSURE_PERCENT_MAX: float = 100.0

# The three settings that feed directly into a sleep duration or a timedelta
# cutoff. Without a ceiling, a stored/submitted finite-but-huge value is just
# as catastrophic as a non-finite one: a multi-year interval sleeps the
# eviction loop for the life of the process, and a multi-century grace/
# retention window overflows ``timedelta`` (its own limit is 999,999,999 days).
EVICTION_INTERVAL_MAX_MINUTES: float = 10080.0  # 7 days -- a maintenance-sweep
# cadence ceiling, not an "off" switch (that is eviction_enabled=False).
# Guarantees the loop wakes at least weekly no matter what is stored.
EVICTION_GRACE_DAYS_MAX: int = 3650  # ~10 years; comfortably under timedelta's limit.
LOG_RETENTION_DAYS_MAX: int = 3650  # ~10 years; same overflow-safety rationale.

# ``log_max_rows`` (issue #152) feeds a row-count cap, not a ``timedelta`` cutoff,
# so it carries no overflow risk of its own -- the ceiling here is purely an
# operator-sanity bound (a fat-fingered huge value degrading to "no cap" is a
# worse failure than a visible 422) and a guard against an unbounded ``OFFSET``
# on every prune tick. 2,000,000 is ~20x the 100,000 default -- generously above
# any install this beta targets, comfortably below anything that would make the
# prune's ordered ``OFFSET`` scan noticeably expensive on SQLite.
LOG_MAX_ROWS_MAX: int = 2_000_000
