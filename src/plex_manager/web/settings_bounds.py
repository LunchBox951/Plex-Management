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
    "EVICTION_GRACE_DAYS_MAX",
    "EVICTION_INTERVAL_MAX_MINUTES",
    "LOG_RETENTION_DAYS_MAX",
]

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
