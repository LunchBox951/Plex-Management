"""Disk-usage percentage math — shared by the health dashboard and eviction.

Both the health dashboard's per-root gauge and the eviction pressure check need
the SAME "how full is this root" number. The adapter/service layer does the only
I/O involved (``shutil.disk_usage()``) and hands the raw byte counts into
:class:`DiskUsage`; this module does nothing but the percentage arithmetic, so
the two features can never disagree about what "90% full" means.

Pure domain: stdlib only. No I/O, no adapter/web imports.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DiskUsage", "used_percent"]


@dataclass(frozen=True)
class DiskUsage:
    """A byte-level snapshot of one configured library root.

    ``root`` is the configured path (a label for the caller — this module never
    touches the filesystem). ``total_bytes`` and ``available_bytes`` are exactly
    ``shutil.disk_usage()``'s ``total``/``free``, read by the caller.
    """

    root: str
    total_bytes: int
    available_bytes: int


def used_percent(usage: DiskUsage) -> float:
    """Return the percentage of ``usage.total_bytes`` currently in use.

    ``0.0`` for a non-positive ``total_bytes`` (an unset or zero-byte
    filesystem) — never divide by zero, and never report a root nobody could
    measure as "full". ``available_bytes`` is not clamped to ``total_bytes``: a
    filesystem can legitimately report more free space than "total" under some
    quota/overlay setups, and clamping would silently hide that rather than
    surface an honest (possibly negative) used percentage.
    """
    if usage.total_bytes <= 0:
        return 0.0
    used_bytes = usage.total_bytes - usage.available_bytes
    return (used_bytes / usage.total_bytes) * 100.0
