"""Tests for the pure two-tier blocklist identity check."""

from __future__ import annotations

from plex_manager.domain.blocklist import BlocklistedRelease, is_blocklisted

_ENTRIES = [
    BlocklistedRelease(
        source_title="Movie.2024.1080p.WEB-DL.x264-BADGRP",
        info_hash="ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        indexer="NiceIndexer",
    ),
    BlocklistedRelease(
        source_title="Other.Movie.2023.720p.BluRay-GRP",
        info_hash=None,
        indexer="OtherIndexer",
    ),
]


def test_hash_hit_case_insensitive() -> None:
    assert (
        is_blocklisted(
            info_hash="abcdef0123456789abcdef0123456789abcdef01",
            source_title="Totally.Different.Title",  # title ignored when hash matches
            indexer="SomeoneElse",
            entries=_ENTRIES,
        )
        is True
    )


def test_title_and_indexer_fallback_when_no_hash() -> None:
    # Candidate has no hash; the entry without a hash matches on title+indexer.
    assert (
        is_blocklisted(
            info_hash=None,
            source_title="other.movie.2023.720p.bluray-grp",  # separator/case noise
            indexer="otherindexer",
            entries=_ENTRIES,
        )
        is True
    )


def test_fallback_used_when_candidate_has_hash_but_entry_does_not() -> None:
    assert (
        is_blocklisted(
            info_hash="ffffffffffffffffffffffffffffffffffffffff",
            source_title="Other.Movie.2023.720p.BluRay-GRP",
            indexer="OtherIndexer",
            entries=_ENTRIES,
        )
        is True
    )


def test_miss_when_nothing_matches() -> None:
    assert (
        is_blocklisted(
            info_hash="0000000000000000000000000000000000000000",
            source_title="Brand.New.Release.2025.1080p.WEB-DL-GRP",
            indexer="FreshIndexer",
            entries=_ENTRIES,
        )
        is False
    )


def test_title_match_but_different_indexer_is_a_miss() -> None:
    assert (
        is_blocklisted(
            info_hash=None,
            source_title="Other.Movie.2023.720p.BluRay-GRP",
            indexer="A.Different.Indexer",
            entries=_ENTRIES,
        )
        is False
    )


def test_empty_entries_never_blocks() -> None:
    assert (
        is_blocklisted(
            info_hash="abcdef0123456789abcdef0123456789abcdef01",
            source_title="Anything",
            indexer="Anywhere",
            entries=[],
        )
        is False
    )
