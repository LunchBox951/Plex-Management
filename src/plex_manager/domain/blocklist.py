"""Blocklist identity check — the pure two-tier "same release" rule.

Ported from Radarr's ``BlocklistService.SameTorrent``: compare by ``info_hash``
(case-insensitive) when both the candidate and the entry expose one, otherwise
fall back to ``source_title`` + ``indexer`` equality. The usenet ``SameNzb`` path
is dropped (torrent-only alpha).

This is a pure function over plain inputs: the caller (a ``BlocklistRepository``
adapter) is responsible for scoping ``entries`` to the relevant media item (by
``tmdb_id``) before calling, so the domain never needs the DB or the tmdb id to
avoid cross-media collisions.

Pure domain: stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["BlocklistedRelease", "is_blocklisted"]


@dataclass(frozen=True)
class BlocklistedRelease:
    """A previously blocklisted release's identity (subset of the DB row)."""

    source_title: str
    info_hash: str | None = None
    indexer: str | None = None


def _normalize_title(title: str) -> str:
    """Lowercase and collapse separator noise so ``A.B-C`` == ``a b c``."""
    return re.sub(r"[\s._-]+", " ", title).strip().casefold()


def _same_indexer(left: str | None, right: str | None) -> bool:
    """Indexer equality (case-insensitive). Both-absent counts as the same."""
    if left is None or right is None:
        return left is None and right is None
    return left.strip().casefold() == right.strip().casefold()


def _matches(
    entry: BlocklistedRelease,
    info_hash: str | None,
    source_title: str,
    indexer: str | None,
) -> bool:
    """Radarr ``SameTorrent``: hash when both present, else title + indexer."""
    if info_hash and entry.info_hash:
        return entry.info_hash.casefold() == info_hash.casefold()
    if not _same_title(source_title, entry.source_title):
        return False
    return _same_indexer(indexer, entry.indexer)


def _same_title(left: str, right: str) -> bool:
    return _normalize_title(left) == _normalize_title(right)


def is_blocklisted(
    *,
    info_hash: str | None,
    source_title: str,
    indexer: str | None,
    entries: Iterable[BlocklistedRelease],
) -> bool:
    """Return True if this release matches any blocklist ``entry``.

    Tier 1 (hash): if the candidate and an entry both carry an ``info_hash``, a
    case-insensitive match blocks. Tier 2 (fallback): when a hash comparison is
    not possible for an entry, a normalized ``source_title`` plus ``indexer``
    match blocks. Either tier matching is a definitive, unconditional skip.
    """
    return any(_matches(entry, info_hash, source_title, indexer) for entry in entries)
