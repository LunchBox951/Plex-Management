"""Correction verbs (ADR-0014): report-issue and cancel.

Exercises the full report-issue flow (blocklist -> remove torrent -> purge file ->
scan -> re-arm -> audit -> inline re-grab) and the cancel flow against the REAL
``LocalFileSystem`` (root guard genuinely exercised), the REAL ``GuessitParser`` +
default quality profile (the decision engine is genuinely run), and fakes only at
the I/O edges (qBittorrent / Prowlarr / Plex).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.prowlarr.adapter import IndexerError, IndexerRateLimitError
from plex_manager.adapters.qbittorrent.adapter import QbittorrentError
from plex_manager.domain.decision_engine import DecisionResult
from plex_manager.domain.quality import WEBDL1080P, QualitySource
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.release import (
    CandidateRelease,
    IndexerSearchRequest,
    ParsedRelease,
    ScoredRelease,
)
from plex_manager.domain.season_pack import MultiSeasonRequestIntent
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import (
    Blocklist,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    DownloadScope,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.download_client import AddResult
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import (
    correction_service,
    grab_service,
    queue_service,
    season_request_service,
)
from plex_manager.services.grab_service import (
    TorrentAlreadyTrackedError,
    TorrentRemovalInFlightError,
)
from plex_manager.services.import_service import PATH_NOT_VISIBLE_REASON_PREFIX
from plex_manager.services.library_roots import LibraryRoots
from tests.web.fakes import FakeLibrary, FakeProwlarr, FakeQbittorrent, candidate

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB = 603
_CULPRIT = "3" * 40
_ALT = "a" * 40


@pytest.fixture(autouse=True)
def clear_removals_in_flight() -> None:
    """Isolate the module-global removal-in-flight registry (#206) between
    tests: a claim a failing test left registered must never refuse an
    unrelated later cancel/grab -- it is process-global state, same discipline
    as ``test_queue_service.py``'s ``clear_operator_claims``."""
    queue_service._removals_in_flight.clear()  # pyright: ignore[reportPrivateUsage]


def _scored(info_hash: str) -> ScoredRelease:
    cand = candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", info_hash=info_hash)
    parsed = ParsedRelease(
        raw_title=cand.title, clean_title="Some Movie", source=QualitySource.WEBDL
    )
    return ScoredRelease(
        candidate=cand, parsed=parsed, quality=WEBDL1080P, profile_index=19, score=1.0
    )


class _FailingProwlarr(FakeProwlarr):
    """A :class:`FakeProwlarr` whose search raises an ``IndexerError`` -- models a
    Prowlarr transport/rate-limit failure hitting the inline report-issue re-search."""

    def __init__(self, exc: IndexerError | None = None) -> None:
        super().__init__([])
        self._exc = exc or IndexerError("prowlarr is down")

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        self.searched.append(request)
        raise self._exc


class _DeleteFailsFileSystem(LocalFileSystem):
    """A root-scoped :class:`LocalFileSystem` whose ``delete`` raises ``OSError`` --
    models a genuine purge failure (permissions / transient I/O) on an IN-ROOT path
    (``contains``/``reclaimable_bytes`` are inherited and behave normally)."""

    def delete(self, path: str) -> None:
        raise OSError("permission denied")


class _AddFailsQbittorrent(FakeQbittorrent):
    """A :class:`FakeQbittorrent` whose ``add`` raises ``QbittorrentError`` -- models the
    download client being unreachable/erroring when the inline report-issue RE-GRAB
    hands it the replacement release. ``remove`` is inherited (the culprit torrent
    removal earlier in the verb still succeeds)."""

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        raise QbittorrentError("qBittorrent is unreachable")


class _EmptyHashQbittorrent(FakeQbittorrent):
    """A :class:`FakeQbittorrent` whose ``add`` ACCEPTS the torrent but returns no
    derivable info-hash -- models the real client accepting an opaque source from which
    no info-hash can be derived (and the indexer supplied none), the exact condition
    ``grab_service`` surfaces as ``GrabError`` (a LIVE, untracked torrent now exists).
    ``remove`` is inherited (the culprit torrent removal earlier still succeeds)."""

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        self.added.append((magnet_or_url, save_path, category))
        return AddResult(torrent_hash="", created=True)


async def _seed_available_movie(
    sm: SessionMaker,
    *,
    library_path: str | None,
    culprit_hash: str = _CULPRIT,
    is_anime: bool = False,
) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.available,
            library_path=library_path,
            is_anime=is_anime,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=culprit_hash,
                status="imported",
                media_request_id=request.id,
                tmdb_id=_TMDB,
                year=2020,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=_TMDB,
                torrent_hash=culprit_hash,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Movie.2020.1080p.BluRay.x264-GROUP",
                indexer="FakeIndexer",
            )
        )
        await session.commit()
        return request.id


async def test_report_issue_movie_blocklists_purges_removes_and_regrabs(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    fs = LocalFileSystem(library_roots=[str(root)])
    library = FakeLibrary()
    # The culprit (BluRay) would OUTRANK the alternative (WEB-DL) if research ran
    # before the blocklist -- so grabbing the alternative proves blocklist-then-
    # research ordering, not merely "some release was grabbed".
    prowlarr = FakeProwlarr(
        [
            candidate("Some.Movie.2020.1080p.BluRay.x264-GROUP", info_hash=_CULPRIT),
            candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT),
        ]
    )

    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            fs,
            library,
            prowlarr,
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    # (c) the library file was purged, (b) the culprit torrent removed WITH data.
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed
    # (d) a Plex scan fired for the removal.
    assert library.scan_calls == [(str(movie_file), "movie")]
    # (g) the inline re-grab landed on 'downloading' -- a replacement is in flight.
    assert updated.status == RequestStatus.downloading.value

    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        downloads = (await session.execute(select(Download))).scalars().all()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.reported
                    )
                )
            )
            .scalars()
            .all()
        )

    # (a) the culprit release is blocklisted (movie-scoped).
    assert len(blocklist) == 1
    assert blocklist[0].torrent_hash == _CULPRIT
    assert blocklist[0].reason == "bad_quality"
    assert blocklist[0].media_type == "movie"
    # blocklist-THEN-research: the re-grab picked the ALTERNATIVE, never the
    # (now-blocklisted) culprit, even though the culprit would rank higher.
    active_hashes = {d.torrent_hash for d in downloads if d.status != "imported"}
    assert active_hashes == {_ALT}
    # (f) an audit row was written.
    assert len(history) == 1


async def test_report_issue_source_error_on_top_replacement_grabs_next(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # The top-ranked REPLACEMENT's HTTP torrent source is unresolvable ->
    # ``qbt.add`` raises QbittorrentSourceError (a QbittorrentError subclass,
    # raised BEFORE anything reaches the client). Pre-fix the
    # _DOWNLOAD_CLIENT_ERRORS catch treated it as a client OUTAGE and left the
    # scope at 'searching' -- the promised synchronous re-grab silently never
    # happened. It is a RELEASE problem: the re-grab must fall through to the
    # next-ranked accepted replacement and grab it (mirroring auto-grab).
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    bad_title = "Some.Movie.2020.1080p.WEB-DL.x264-BAD"
    qbt = FakeQbittorrent(source_errors={f"http://idx.local/{bad_title}"})
    prowlarr = FakeProwlarr(
        [
            # The culprit is blocklisted by the verb itself before the re-search.
            candidate("Some.Movie.2020.1080p.BluRay.x264-GROUP", info_hash=_CULPRIT),
            # Top-ranked replacement: accepted, but its only source is an HTTP
            # url the client's source resolution vetoes.
            candidate(bad_title, magnet=False, seeders=100),
            # Lower-ranked replacement: grabs cleanly.
            candidate("Some.Movie.2020.1080p.WEB-DL.x264-ALT", info_hash=_ALT, seeders=10),
        ]
    )

    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            prowlarr,
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    # Never left at 'searching' with no action: the fallback replacement is in flight.
    assert updated.status == RequestStatus.downloading.value
    async with sessionmaker_() as session:
        downloads = (await session.execute(select(Download))).scalars().all()
    active_hashes = {d.torrent_hash for d in downloads if d.status != "imported"}
    assert active_hashes == {_ALT}


async def test_report_issue_source_error_exhaustion_parks_honestly(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # EVERY accepted replacement has an unresolvable source: the bounded
    # fall-through exhausts and the scope parks on the honest, retryable
    # no_acceptable_release -- never the pre-fix silent 'searching' limbo
    # (worst with auto-grab disabled: nothing would ever retry it).
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    bad_title = "Some.Movie.2020.1080p.WEB-DL.x264-BAD"
    qbt = FakeQbittorrent(source_errors={f"http://idx.local/{bad_title}"})
    prowlarr = FakeProwlarr(
        [
            candidate("Some.Movie.2020.1080p.BluRay.x264-GROUP", info_hash=_CULPRIT),
            candidate(bad_title, magnet=False, seeders=100),
        ]
    )

    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            prowlarr,
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    assert updated.status == RequestStatus.no_acceptable_release.value
    async with sessionmaker_() as session:
        downloads = (await session.execute(select(Download))).scalars().all()
    assert {d.torrent_hash for d in downloads if d.status != "imported"} == set()


async def test_report_issue_regrab_removal_in_flight_leaves_scope_at_searching(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Finding 2 (#206): the inline report-issue re-grab's replacement resolves to a
    hash whose terminal row is being removed RIGHT NOW by a racing cancel/reconcile/
    operator delete, so ``grab`` raises ``TorrentRemovalInFlightError``. That outcome
    must be treated like the other transient scope refusals -- the already-committed
    report flow LEAVES the scope at ``searching`` for the auto-grab worker to retry --
    NOT bubble an unhandled 500 out of the requests router. Pre-fix the exception was
    in none of the caught families and escaped."""
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    # An UNRELATED terminal (failed) download already owns the replacement's hash and
    # is mid-removal (a cancel that committed terminal + registered its guard). The
    # re-grab's only accepted replacement resolves to exactly that hash.
    async with sessionmaker_() as session:
        other = MediaRequest(
            tmdb_id=700, media_type=MediaType.movie, title="Other", status=RequestStatus.cancelled
        )
        session.add(other)
        await session.flush()
        alt_download = Download(
            torrent_hash=_ALT,
            status="failed",
            media_request_id=other.id,
            tmdb_id=700,
            failed_reason="cancelled by operator",
        )
        session.add(alt_download)
        await session.commit()
        alt_download_id = alt_download.id
        other_id = other.id

    qbt = FakeQbittorrent()
    prowlarr = FakeProwlarr(
        [
            candidate("Some.Movie.2020.1080p.BluRay.x264-GROUP", info_hash=_CULPRIT),
            candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT),
        ]
    )

    queue_service.register_removal_in_flight(alt_download_id)
    try:
        async with sessionmaker_() as session:
            updated = await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                prowlarr,
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(root)),
            )
    finally:
        queue_service.release_removal_in_flight(alt_download_id)

    # No 500 escaped: the report committed, and the scope is LEFT at 'searching' for
    # the auto-grab worker to retry once the removal settles -- never parked
    # 'no_acceptable_release' (releases exist), never a silent success.
    assert updated.status == RequestStatus.searching.value
    async with sessionmaker_() as session:
        downloads = (await session.execute(select(Download))).scalars().all()
        alt_row = await session.get(Download, alt_download_id)
    # The guard fired BEFORE any add: nothing was handed to the client for _ALT, and
    # the guarded terminal row is untouched (still failed, still owned by the other).
    assert qbt.added == []
    assert {d.torrent_hash for d in downloads if d.status != "imported"} == {_ALT}
    assert alt_row is not None
    assert alt_row.status == "failed"
    assert alt_row.media_request_id == other_id


async def test_report_issue_movie_reset_clears_library_path(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 1024)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            FakeQbittorrent(),
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            # Nothing acceptable -> parks; the reset/clear still must have happened.
            FakeProwlarr([]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="user_reported",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None
    # Nothing acceptable after the blocklist -> honest, retryable park; the stale
    # library_path/completed anchors were cleared (the file is gone).
    assert request.status == RequestStatus.no_acceptable_release.value
    assert request.library_path is None
    assert request.completed_at is None


async def test_report_issue_refuses_when_media_root_unmounted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # Foot-gun failsafe: an unmounted/empty root aborts the WHOLE verb -- nothing
    # is blocklisted, removed, purged, or flipped (the file isn't really gone). The
    # root to verify is DERIVED from the stored breadcrumb (ADR-0015 fix): the
    # library_path sits under ``root``, which is an EMPTY stub dir (a freshly-
    # unmounted mountpoint), so the mount check on THAT root trips.
    root = tmp_path / "movies"
    root.mkdir()  # exists but EMPTY -> reads as "not mounted"
    movie_file = root / "Some Movie (2020).mkv"  # never written -> the drive is gone
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.MediaRootUnavailableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(root)),
            )

    assert qbt.removed == []  # nothing was removed
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None
    assert request.status == RequestStatus.available.value  # untouched
    assert blocklist == []


async def test_report_issue_legacy_anime_breadcrumb_under_movies_root_does_not_refuse(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING 1 (a): an anime title imported BEFORE an anime root was configured has
    # its library_path under movies_root. With an anime root NOW configured but EMPTY
    # (freshly created), the OLD is_anime-based pick checked that empty anime root and
    # spuriously 409'd. The fix derives the check-root from the breadcrumb -> movies_root
    # (mounted, non-empty) -> the verb proceeds and purges the real file.
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()  # configured but EMPTY (nothing ever imported here)
    movie_file = movies_root / "Some Anime Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(movies_root), str(anime_root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            # Both roots configured; the breadcrumb is under movies_root, not anime_root.
            roots=LibraryRoots(movies=str(movies_root), anime_movie=str(anime_root)),
        )

    # No spurious refusal: the real file (under movies_root) was purged and re-grabbed.
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed
    assert updated.status == RequestStatus.downloading.value


async def test_report_issue_trips_failsafe_when_the_breadcrumbs_own_root_is_unmounted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING 1 (b): the breadcrumb is under movies_root, which is MISSING (unmounted),
    # while a DIFFERENT root (anime_root) happens to be mounted. The OLD is_anime pick
    # verified the mounted anime root, waved the check through, and then the purge no-op'd
    # on the not-present file -> blocklist + re-grab a duplicate, STRANDING the old file
    # once the drive returns. The fix verifies the breadcrumb's REAL root -> refuses.
    movies_root = tmp_path / "movies"  # never created -> unmounted
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    (anime_root / "keep").write_bytes(b"x")  # a DIFFERENT, mounted root
    movie_file = movies_root / "Some Anime Movie (2020).mkv"
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.MediaRootUnavailableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(movies_root), str(anime_root)]),
                FakeLibrary(),
                FakeProwlarr(
                    [candidate("Some.Anime.Movie.2020.1080p.WEB-DL.x264", info_hash=_ALT)]
                ),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(movies_root), anime_movie=str(anime_root)),
            )

    # The failsafe fired: nothing removed, the request untouched, no blocklist row.
    assert qbt.removed == []
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None and request.status == RequestStatus.available.value
    assert blocklist == []


async def test_report_issue_refuses_when_breadcrumb_is_outside_every_configured_root(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING 1 (c): the stored breadcrumb resolves under NONE of the configured roots
    # (a stale/misconfigured path, or a root removed from config). Fail HONESTLY up front
    # -- a visible, correctable refusal -- never a silent blocklist + re-grab against a
    # file we cannot even locate to purge (which would strand it).
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    (movies_root / "keep").write_bytes(b"x")  # mounted, non-empty
    orphan = tmp_path / "somewhere-else"
    orphan.mkdir()
    movie_file = orphan / "Some Movie (2020).mkv"  # under NO configured root
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.MediaRootUnavailableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(movies_root)]),
                FakeLibrary(),
                FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264", info_hash=_ALT)]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(movies_root)),
            )

    # Honest refusal before any side effect: file intact, nothing removed/blocklisted.
    assert movie_file.exists()
    assert qbt.removed == []
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None and request.status == RequestStatus.available.value
    assert blocklist == []


async def test_report_issue_verifies_the_nested_child_root_not_its_mounted_parent(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING A (nested roots): anime_movie_root is a CHILD MOUNT inside movies_root.
    # The child mount is DOWN (empty stub dir, the anime file gone with it) while the
    # parent movies_root is mounted and non-empty. First-match root selection picked
    # the parent, passed the failsafe, and the purge then no-op'd on the missing child
    # path -- blocklisting + re-grabbing + clearing the breadcrumb while the original
    # file quietly returns with the mount. Deepest-match must verify the CHILD and
    # refuse.
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    (movies_root / "keep").write_bytes(b"x")  # parent mounted, non-empty
    anime_root = movies_root / "anime"
    anime_root.mkdir()  # exists but EMPTY -> the child mount is down
    movie_file = anime_root / "Some Anime (2020).mkv"  # never written -> gone with it
    request_id = await _seed_available_movie(
        sessionmaker_, library_path=str(movie_file), is_anime=True
    )

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.MediaRootUnavailableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(movies_root), str(anime_root)]),
                FakeLibrary(),
                FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264", info_hash=_ALT)]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(movies_root), anime_movie=str(anime_root)),
            )

    # The failsafe fired on the CHILD root: nothing blocklisted/removed/re-armed.
    assert qbt.removed == []
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None and request.status == RequestStatus.available.value
    assert blocklist == []


async def test_report_issue_purges_under_a_mounted_nested_anime_root(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING A's positive twin: the nested child root is UP and holds the file --
    # deepest-match verifies the child (mounted, non-empty) and the verb proceeds,
    # purging the real file. Guards against over-refusing nested layouts.
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = movies_root / "anime"
    anime_root.mkdir()
    movie_file = anime_root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(
        sessionmaker_, library_path=str(movie_file), is_anime=True
    )

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(movies_root), str(anime_root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(movies_root), anime_movie=str(anime_root)),
        )

    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed
    assert updated.status == RequestStatus.downloading.value


async def test_report_issue_without_breadcrumb_still_checks_the_media_root(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING C: a row with NO library_path breadcrumb (predating the column, or
    # recorded available straight from Plex) has no path to derive an owner from --
    # but the pre-ADR-0015-fix failsafe still mount-checked the media root BEFORE any
    # side effect. Skipping the check entirely would let a report against an UNMOUNTED
    # library blocklist the release and re-grab a duplicate of a file that is still
    # really there once the drive returns. The fallback check must refuse.
    missing_root = tmp_path / "movies"  # never created -> unmounted
    request_id = await _seed_available_movie(sessionmaker_, library_path=None)

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.MediaRootUnavailableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(missing_root)]),
                FakeLibrary(),
                FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264", info_hash=_ALT)]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(missing_root)),
            )

    # Refused before ANY side effect: nothing blocklisted, no torrent removed.
    assert qbt.removed == []
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert request is not None and request.status == RequestStatus.available.value
    assert blocklist == []


async def test_report_issue_without_breadcrumb_proceeds_when_the_root_is_mounted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING C's positive twin: the no-breadcrumb fallback check passes on a mounted
    # non-empty root and the verb proceeds -- blocklist + torrent removal + re-grab,
    # with the purge honestly skipped (nothing of ours to delete). Guards the fallback
    # against over-refusing.
    root = tmp_path / "movies"
    root.mkdir()
    (root / "keep").write_bytes(b"x")  # mounted, non-empty
    request_id = await _seed_available_movie(sessionmaker_, library_path=None)

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    assert (_CULPRIT, True) in qbt.removed  # the culprit torrent still goes
    assert updated.status == RequestStatus.downloading.value


async def test_report_issue_without_breadcrumb_falls_back_to_the_anime_root_for_anime(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # FINDING C, anime variant: the fallback for an is_anime row prefers the configured
    # anime root (the root its file most plausibly lives under -- the import router's
    # own pick), so an anime report proceeds off a mounted anime root even while
    # movies_root is down.
    missing_movies_root = tmp_path / "movies"  # never created -> unmounted
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    (anime_root / "keep").write_bytes(b"x")  # mounted, non-empty
    request_id = await _seed_available_movie(sessionmaker_, library_path=None, is_anime=True)

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(missing_movies_root), str(anime_root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(missing_movies_root), anime_movie=str(anime_root)),
        )

    assert updated.status == RequestStatus.downloading.value


async def test_report_issue_presence_only_no_culprit_proceeds_despite_unmounted_root(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # Issue #131 relaxation: a row with NEITHER a library_path breadcrumb NOR a
    # culprit download is purely presence-derived (recorded available straight
    # from Plex -- no download of ours ever placed it). There is nothing to
    # blocklist (culprit is None) and nothing to purge (no breadcrumb), so an
    # unmounted fallback root protects no file of ours: the mount check must be
    # SKIPPED and the verb must proceed to the honest re-arm + re-search, not
    # 409 `media_root_unavailable` -- the exact dead-end reported in #131.
    #
    # Seeded directly (NOT via `_seed_available_movie`, which always adds a
    # culprit `Download` row): this row has neither.
    missing_root = tmp_path / "movies"  # never created -> unmounted
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.available,
            library_path=None,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(missing_root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(missing_root)),
        )

    assert updated.status == RequestStatus.downloading.value
    assert qbt.removed == []  # nothing of ours to remove -- no culprit torrent
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert blocklist == []  # nothing to blocklist -- culprit was None


async def test_report_issue_rejects_a_not_reportable_movie(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    (root / "keep").write_bytes(b"x")  # non-empty so the failsafe wouldn't fire
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.searching,  # not imported/available -> not reportable
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    with pytest.raises(correction_service.NotReportableError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                FakeQbittorrent(),
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(root)),
            )


async def test_report_issue_tv_season_purges_and_parks_on_nothing_acceptable(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    tv_root = tmp_path / "tv"
    season_dir = tv_root / "Some Show" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)

    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=show.id,
                season_number=1,
                status=RequestStatus.available,
                library_path=str(season_dir),
            )
        )
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1399,
                season=1,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=1399,
                torrent_hash=_CULPRIT,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Show.S01.1080p.WEB-DL.x264-GROUP",
                indexer="FakeIndexer",
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            # Only the culprit is on offer -> blocklisted -> nothing acceptable ->
            # the season parks honestly (also proves blocklist-then-research).
            FakeProwlarr([candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash=_CULPRIT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="wrong_media",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    assert not season_dir.exists()  # the season tree was purged
    assert (_CULPRIT, True) in qbt.removed

    async with sessionmaker_() as session:
        season = (
            await session.execute(
                select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
            )
        ).scalar_one()
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    # The season re-armed, found nothing acceptable (only the blocklisted culprit),
    # and parked honestly; its purge breadcrumb was cleared.
    assert season.status == RequestStatus.no_acceptable_release
    assert season.library_path is None
    assert len(blocklist) == 1
    assert blocklist[0].media_type == "tv"


async def _seed_partial_shared_pack(
    sm: SessionMaker,
    *,
    tv_root: Path,
    season_01_dir: Path,
    season_02_dir: Path | None = None,
    season_1_status: RequestStatus = RequestStatus.available,
    season_2_status: RequestStatus = RequestStatus.import_blocked,
    download_status: str = DownloadState.ImportBlocked.value,
    season_1_scope_status: str = "imported",
    season_2_scope_status: str = "import_blocked",
) -> tuple[int, int, int, int]:
    """Seed a partial multi-season pack: one shared ``_CULPRIT`` torrent, season 1
    imported/available, season 2 still non-terminal. Returns
    ``(request_id, download_id, season1_id, season2_id)``."""
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(show)
        await session.flush()
        season_1 = SeasonRequest(
            media_request_id=show.id,
            season_number=1,
            status=season_1_status,
            library_path=str(season_01_dir),
        )
        season_2 = SeasonRequest(
            media_request_id=show.id,
            season_number=2,
            status=season_2_status,
            library_path=str(season_02_dir) if season_02_dir is not None else None,
        )
        session.add_all([season_1, season_2])
        await session.flush()
        download = Download(
            torrent_hash=_CULPRIT,
            status=download_status,
            media_request_id=show.id,
            tmdb_id=1399,
            season=1,
        )
        session.add(download)
        await session.flush()
        session.add_all(
            [
                DownloadScope(
                    download_id=download.id,
                    media_request_id=show.id,
                    season_request_id=season_1.id,
                    season_number=1,
                    scope_key="season:1|episodes:*",
                    status=season_1_scope_status,
                ),
                DownloadScope(
                    download_id=download.id,
                    media_request_id=show.id,
                    season_request_id=season_2.id,
                    season_number=2,
                    scope_key="season:2|episodes:*",
                    status=season_2_scope_status,
                ),
                DownloadHistory(
                    tmdb_id=1399,
                    torrent_hash=_CULPRIT,
                    event_type=DownloadHistoryEvent.grabbed,
                    source_title="Some.Show.S01.1080p.WEB-DL.x264-GROUP",
                    indexer="FakeIndexer",
                ),
            ]
        )
        await session.commit()
        return show.id, download.id, season_1.id, season_2.id


async def test_report_issue_rescues_a_shared_pack_sibling_season(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    tv_root = tmp_path / "tv"
    season_01_dir = tv_root / "Some Show" / "Season 01"
    season_01_dir.mkdir(parents=True)
    (season_01_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)

    request_id, download_id, _season1_id, _season2_id = await _seed_partial_shared_pack(
        sessionmaker_, tv_root=tv_root, season_01_dir=season_01_dir
    )

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            # Only the culprit release is on offer -> blocklisted -> the reported
            # season parks; the sibling rescue is unrelated to what is on offer.
            FakeProwlarr([candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash=_CULPRIT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="wrong_media",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    assert (_CULPRIT, True) in qbt.removed
    assert not season_01_dir.exists()

    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest)
                    .where(SeasonRequest.media_request_id == request_id)
                    .order_by(SeasonRequest.season_number)
                )
            )
            .scalars()
            .all()
        )
        season_1, season_2 = seasons
        download = await session.get(Download, download_id)
        assert download is not None
        scopes = (
            (
                await session.execute(
                    select(DownloadScope).where(DownloadScope.download_id == download_id)
                )
            )
            .scalars()
            .all()
        )
        scope_by_season = {scope.season_number: scope for scope in scopes}
        blocklist = (await session.execute(select(Blocklist))).scalars().all()

    assert season_2.status == RequestStatus.searching  # rescued, not orphaned
    assert season_1.status == RequestStatus.no_acceptable_release  # parked honestly
    assert download.status == DownloadState.Failed.value  # no zombie import_blocked row
    assert scope_by_season[2].status == "failed"  # terminalized
    assert scope_by_season[1].status == "imported"  # untouched
    assert len(blocklist) == 1
    assert blocklist[0].torrent_hash == _CULPRIT
    assert blocklist[0].media_type == "tv"


async def test_report_issue_shared_pack_sibling_scope_becomes_reattachable(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    tv_root = tmp_path / "tv"
    season_01_dir = tv_root / "Some Show" / "Season 01"
    season_01_dir.mkdir(parents=True)
    (season_01_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)

    request_id, _download_id, _season1_id, season2_id = await _seed_partial_shared_pack(
        sessionmaker_, tv_root=tv_root, season_01_dir=season_01_dir
    )

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash=_CULPRIT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="wrong_media",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    async with sessionmaker_() as session:
        new_download = Download(
            torrent_hash="b" * 40,
            status=DownloadState.Downloading.value,
            media_request_id=request_id,
            tmdb_id=1399,
            season=2,
        )
        session.add(new_download)
        await session.flush()
        session.add(
            DownloadScope(
                download_id=new_download.id,
                media_request_id=request_id,
                season_request_id=season2_id,
                season_number=2,
                scope_key="season:2|episodes:*",
                status="active",
            )
        )
        # Must NOT raise IntegrityError on uq_download_scopes_active_scope -- the
        # old sibling scope was terminalized to 'failed' by the rescue.
        await session.flush()

        old_scope = (
            await session.execute(
                select(DownloadScope).where(
                    DownloadScope.media_request_id == request_id,
                    DownloadScope.season_number == 2,
                    DownloadScope.download_id != new_download.id,
                )
            )
        ).scalar_one()
        assert old_scope.status == "failed"


async def test_report_issue_does_not_rearm_an_already_imported_sibling(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    tv_root = tmp_path / "tv"
    season_01_dir = tv_root / "Some Show" / "Season 01"
    season_02_dir = tv_root / "Some Show" / "Season 02"
    season_01_dir.mkdir(parents=True)
    season_02_dir.mkdir(parents=True)
    (season_01_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)
    (season_02_dir / "Some.Show.S02E01.mkv").write_bytes(b"x" * 2048)

    request_id, download_id, _season1_id, _season2_id = await _seed_partial_shared_pack(
        sessionmaker_,
        tv_root=tv_root,
        season_01_dir=season_01_dir,
        season_02_dir=season_02_dir,
        season_2_status=RequestStatus.available,
        download_status="imported",
        season_2_scope_status="imported",
    )

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash=_CULPRIT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="wrong_media",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    assert (_CULPRIT, True) in qbt.removed

    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest)
                    .where(SeasonRequest.media_request_id == request_id)
                    .order_by(SeasonRequest.season_number)
                )
            )
            .scalars()
            .all()
        )
        season_1, season_2 = seasons
        download = await session.get(Download, download_id)
        assert download is not None
        scope_2 = (
            await session.execute(
                select(DownloadScope).where(
                    DownloadScope.download_id == download_id,
                    DownloadScope.season_number == 2,
                )
            )
        ).scalar_one()

    assert season_1.status == RequestStatus.no_acceptable_release
    assert season_2.status == RequestStatus.available  # untouched -- no spurious re-arm
    assert scope_2.status == "imported"
    assert download.status == "imported"  # CAS gated on {ImportBlocked} no-ops


async def test_report_issue_does_not_rearm_a_sibling_completed_by_a_racing_import(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # P2 hardening: the rescue's ``siblings`` list is built from the ``culprit.scopes``
    # DTO snapshot, read BEFORE the rescue's own writes. Simulate a concurrent import
    # retry that completes season 2 (its DownloadScope -> 'imported') in the gap
    # between that snapshot and the rescue's per-sibling compare-and-set: the season-2
    # scope-level CAS must lose (the scope is no longer in the non-terminal set) and
    # the season must NOT be re-armed -- re-arming already-available content back to
    # 'searching' risks a needless duplicate re-grab. The download-ROW CAS still wins
    # (untouched by the race), so the rescue proceeds for the row but must still
    # individually honor the sibling's own, separately-raced status.
    tv_root = tmp_path / "tv"
    season_01_dir = tv_root / "Some Show" / "Season 01"
    season_01_dir.mkdir(parents=True)
    (season_01_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)

    request_id, download_id, _season1_id, _season2_id = await _seed_partial_shared_pack(
        sessionmaker_, tv_root=tv_root, season_01_dir=season_01_dir
    )

    real_update_status_if_in = SqlDownloadRepository.update_status_if_in
    raced = {"done": False}

    async def racing_update_status_if_in(
        self: SqlDownloadRepository,
        download_id_: int,
        status: str,
        allowed_from: frozenset[str],
        *,
        failed_reason: str | None = None,
        clear_download_path: bool = False,
    ) -> bool:
        # Matches ONLY the rescue's own download-row CAS (this exact target status +
        # predicate) so a later, unrelated CAS in the same flow (e.g. the inline
        # replacement grab's row reuse) is never intercepted. The keyword-only
        # params mirror exactly what the rescue's call site passes -- no ``**kwargs``
        # forwarding, so this stays typed under strict pyright.
        if (
            not raced["done"]
            and download_id_ == download_id
            and status == DownloadState.Failed.value
            and allowed_from == frozenset({DownloadState.ImportBlocked.value})
        ):
            raced["done"] = True
            async with sessionmaker_() as competitor:
                scope = (
                    await competitor.execute(
                        select(DownloadScope).where(
                            DownloadScope.download_id == download_id_,
                            DownloadScope.season_number == 2,
                        )
                    )
                ).scalar_one()
                scope.status = "imported"
                await competitor.commit()
        return await real_update_status_if_in(
            self,
            download_id_,
            status,
            allowed_from,
            failed_reason=failed_reason,
            clear_download_path=clear_download_path,
        )

    monkeypatch.setattr(SqlDownloadRepository, "update_status_if_in", racing_update_status_if_in)

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            FakeProwlarr([candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash=_CULPRIT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="wrong_media",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    assert (_CULPRIT, True) in qbt.removed
    assert raced["done"]  # the race actually fired -- a false pass would prove nothing

    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest)
                    .where(SeasonRequest.media_request_id == request_id)
                    .order_by(SeasonRequest.season_number)
                )
            )
            .scalars()
            .all()
        )
        season_1, season_2 = seasons
        download = await session.get(Download, download_id)
        assert download is not None
        scope_2 = (
            await session.execute(
                select(DownloadScope).where(
                    DownloadScope.download_id == download_id,
                    DownloadScope.season_number == 2,
                )
            )
        ).scalar_one()

    assert season_1.status == RequestStatus.no_acceptable_release  # parked honestly
    # NOT re-armed: the racing import's completion beat the rescue's scope-level CAS.
    assert season_2.status == RequestStatus.import_blocked  # untouched, original status
    assert scope_2.status == "imported"  # the race's write stands -- never overwritten
    assert download.status == DownloadState.Failed.value  # row CAS still won independently


async def test_report_issue_tv_requires_a_season(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    root = tmp_path / "tv"
    root.mkdir()
    (root / "keep").write_bytes(b"x")
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(media_request_id=show.id, season_number=1, status=RequestStatus.available)
        )
        await session.commit()
        request_id = show.id

    with pytest.raises(correction_service.ReportSeasonRequiredError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                FakeQbittorrent(),
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([]),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(tv=str(root)),
            )


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #
async def test_cancel_movie_removes_torrent_and_settles_cancelled(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)

    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed
    async with sessionmaker_() as session:
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _CULPRIT))
        ).scalar_one()
        history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.cancelled
                    )
                )
            )
            .scalars()
            .all()
        )
    assert download.status == "failed"
    assert download.failed_reason == "cancelled by operator"
    assert len(history) == 1


async def test_cancel_already_gone_torrent_is_a_no_op_success(
    sessionmaker_: SessionMaker,
) -> None:
    # The download's torrent isn't in the client (already gone): remove is a no-op
    # success and the cancel still settles cleanly.
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    # FakeQbittorrent with NO statuses -> the hash is "already gone"; remove() still
    # succeeds (mirrors qBittorrent's /torrents/delete tolerating an unknown hash).
    qbt = FakeQbittorrent(statuses=[])
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)
    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed


async def test_cancel_tv_refuses_when_a_season_is_already_available(
    sessionmaker_: SessionMaker,
) -> None:
    # season_rollup precedence makes the parent read `downloading` even though
    # season 1 is already `available` (imported, file on disk, torrent seeding). A
    # naive cancel would settle season 1 `cancelled` -- orphaning its seeding
    # torrent (excluded from the active sweep) and its file (eviction ignores
    # `cancelled`). Cancel must REFUSE and touch nothing.
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1400,
            media_type=MediaType.tv,
            title="Mixed Show",
            status=RequestStatus.downloading,  # rollup of {available, downloading}
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(media_request_id=show.id, season_number=1, status=RequestStatus.available)
        )
        session.add(
            SeasonRequest(
                media_request_id=show.id, season_number=2, status=RequestStatus.downloading
            )
        )
        # Season 1's imported (terminal) download -- torrent still seeding.
        session.add(
            Download(
                torrent_hash=_ALT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1400,
                season=1,
            )
        )
        # Season 2's in-flight download.
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=show.id,
                tmdb_id=1400,
                season=2,
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.NotCancellableError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(session, qbt, request_id=request_id)

    # Nothing removed, no season settled cancelled: the done season is untouched.
    assert qbt.removed == []
    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
    assert {s.status for s in seasons} == {RequestStatus.available, RequestStatus.downloading}


async def test_cancel_tv_refuses_when_a_season_is_evicted_but_still_seeding(
    sessionmaker_: SessionMaker,
) -> None:
    # An `evicted` season's status does not read done, but it can still own an
    # imported download whose torrent seeds (eviction deletes only the library
    # file). The parent rolls up to `downloading` via precedence, so the rollup
    # guard passes -- the per-season imported-download probe must still refuse.
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1401,
            media_type=MediaType.tv,
            title="Evicted Show",
            status=RequestStatus.downloading,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(media_request_id=show.id, season_number=1, status=RequestStatus.evicted)
        )
        session.add(
            SeasonRequest(
                media_request_id=show.id, season_number=2, status=RequestStatus.downloading
            )
        )
        session.add(
            Download(
                torrent_hash=_ALT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1401,
                season=1,
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.NotCancellableError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(session, qbt, request_id=request_id)
    assert qbt.removed == []


async def test_cancel_tv_settles_every_season_and_rolls_up_cancelled(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.downloading,
        )
        session.add(show)
        await session.flush()
        season_1 = SeasonRequest(
            media_request_id=show.id, season_number=1, status=RequestStatus.downloading
        )
        session.add(season_1)
        session.add(
            SeasonRequest(media_request_id=show.id, season_number=2, status=RequestStatus.searching)
        )
        download = Download(
            torrent_hash=_CULPRIT,
            status="downloading",
            media_request_id=show.id,
            tmdb_id=1399,
            season=1,
        )
        session.add(download)
        await session.flush()
        session.add(
            DownloadScope(
                download_id=download.id,
                media_request_id=show.id,
                season_request_id=season_1.id,
                season_number=1,
                scope_key="season:1|episodes:*",
                status="active",
            )
        )
        await session.commit()
        request_id, download_id = show.id, download.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)

    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed
    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .all()
        )
        scope = (
            await session.execute(
                select(DownloadScope).where(DownloadScope.download_id == download_id)
            )
        ).scalar_one()
    assert {s.status for s in seasons} == {RequestStatus.cancelled}
    assert scope.status == "cancelled"


async def test_cancel_terminalizes_import_blocked_scope_so_replacement_can_attach(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1400,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.downloading,
        )
        session.add(show)
        await session.flush()
        season_1 = SeasonRequest(
            media_request_id=show.id, season_number=1, status=RequestStatus.downloading
        )
        season_2 = SeasonRequest(
            media_request_id=show.id, season_number=2, status=RequestStatus.import_blocked
        )
        session.add_all([season_1, season_2])
        await session.flush()
        download = Download(
            torrent_hash=_CULPRIT,
            status=DownloadState.ImportBlocked.value,
            media_request_id=show.id,
            tmdb_id=1400,
            season=1,
        )
        session.add(download)
        await session.flush()
        session.add(
            DownloadScope(
                download_id=download.id,
                media_request_id=show.id,
                season_request_id=season_2.id,
                season_number=2,
                scope_key="season:2|episodes:*",
                status="import_blocked",
            )
        )
        await session.commit()
        request_id, season_id, download_id = show.id, season_2.id, download.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)

    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed
    async with sessionmaker_() as session:
        scope = (
            await session.execute(
                select(DownloadScope).where(DownloadScope.download_id == download_id)
            )
        ).scalar_one()
        replacement = Download(
            torrent_hash="b" * 40,
            status=DownloadState.Downloading.value,
            media_request_id=request_id,
            tmdb_id=1400,
            season=2,
        )
        session.add(replacement)
        await session.flush()
        session.add(
            DownloadScope(
                download_id=replacement.id,
                media_request_id=request_id,
                season_request_id=season_id,
                season_number=2,
                scope_key="season:2|episodes:*",
                status="active",
            )
        )
        await session.flush()

    assert scope.status == "cancelled"


async def test_cancel_rejects_an_already_imported_request(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.available,  # already imported -> not cancellable
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    with pytest.raises(correction_service.NotCancellableError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(
                session, FakeQbittorrent(), request_id=request_id
            )


async def test_cancelled_request_no_longer_blocks_a_fresh_request(
    sessionmaker_: SessionMaker,
) -> None:
    # A cancelled row is SETTLED (outside uq_media_requests_active): a brand-new
    # active request for the same media must be insertable alongside it.
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        await correction_service.cancel_request(session, FakeQbittorrent(), request_id=request_id)

    async with sessionmaker_() as session:
        fresh = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.pending,
        )
        session.add(fresh)
        await session.commit()  # must NOT raise IntegrityError
        assert fresh.id != request_id


# --------------------------------------------------------------------------- #
# Codex round: correction-semantics hardening (PR #32)
# --------------------------------------------------------------------------- #
async def test_report_issue_blocklists_the_imported_culprit_not_a_newer_failed_attempt(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # A season available with an OLD imported download (owns the file) plus a NEWER
    # failed supplementary attempt for the same season. report-issue must blocklist +
    # remove the IMPORTED torrent (the seed hardlinking the file), never the newer
    # failed row -- otherwise the real seed keeps holding the file and the purge frees
    # nothing (ADR-0014).
    tv_root = tmp_path / "tv"
    season_dir = tv_root / "Some Show" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)
    imported_hash = _ALT
    failed_hash = _CULPRIT

    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399, media_type=MediaType.tv, title="Some Show", status=RequestStatus.available
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=show.id,
                season_number=1,
                status=RequestStatus.available,
                library_path=str(season_dir),
            )
        )
        # OLD imported row (lower id) -- the real seed of the placed file.
        session.add(
            Download(
                torrent_hash=imported_hash,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1399,
                season=1,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=1399,
                torrent_hash=imported_hash,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Show.S01.1080p.WEB-DL.x264-GROUP",
                indexer="FakeIndexer",
            )
        )
        # NEWER failed supplementary row (higher id) -- must NOT be picked as culprit.
        session.add(
            Download(
                torrent_hash=failed_hash,
                status="failed",
                media_request_id=show.id,
                tmdb_id=1399,
                season=1,
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            FakeProwlarr([]),  # nothing acceptable -> parks; culprit resolution is the point
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    # The IMPORTED torrent (the seed) was removed WITH data, never the failed row's.
    assert (imported_hash, True) in qbt.removed
    assert (failed_hash, True) not in qbt.removed
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
    assert len(blocklist) == 1
    assert blocklist[0].torrent_hash == imported_hash


async def test_report_issue_refuses_when_an_active_sibling_owns_the_dedup_slot(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # An older SETTLED available request coexists with a NEWER active one for the same
    # media (allowed -- the partial unique index only constrains active rows). Reporting
    # the settled one would re-arm it active and collide on that index AFTER the
    # irreversible purge already ran. Refuse UP FRONT: nothing blocklisted/removed/purged.
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 1024)
    settled_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))
    async with sessionmaker_() as session:
        sibling = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.searching,  # a NEWER active request for the same media
        )
        session.add(sibling)
        await session.commit()

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.ActiveDuplicateError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264", info_hash=_ALT)]),
                GuessitParser(),
                default_profile(),
                request_id=settled_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(root)),
            )

    # Nothing touched: the file is still there, no torrent removed, no blocklist row.
    assert movie_file.exists()
    assert qbt.removed == []
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        settled = await session.get(MediaRequest, settled_id)
    assert blocklist == []
    assert settled is not None and settled.status == RequestStatus.available


async def test_report_issue_leaves_scope_searching_when_the_indexer_fails_during_research(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # Issue #71: the inline re-search hits a Prowlarr transport/rate-limit failure AFTER
    # the blocklist/purge already committed. This is an OPERATIONAL failure (the indexer
    # is unreachable), NOT content exhaustion -- mirroring auto-grab's raised-search
    # taxonomy, it must NOT park no_acceptable_release (a LIE: releases may exist) and
    # must NOT propagate a 5xx. The scope is LEFT at the 'searching' committed at (b)
    # for the merged auto-grab worker to retry.
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 1024)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            _FailingProwlarr(IndexerRateLimitError("every indexer rate-limited")),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    # The purge + blocklist still happened; the indexer failure did NOT propagate and
    # did NOT dishonestly park -- the scope self-heals at 'searching'.
    assert updated.status == RequestStatus.searching.value
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        downloads = (await session.execute(select(Download))).scalars().all()
    assert len(blocklist) == 1
    # The failed re-search created no new active download row -- only the terminal culprit.
    assert {d.status for d in downloads} == {"imported"}


async def test_report_issue_preserves_the_breadcrumb_when_the_purge_fails(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # A genuine delete failure (permissions / I/O) leaves the file on disk. The
    # library_path breadcrumb -- the only handle a later retry/eviction has to reclaim
    # the orphan -- must be PRESERVED, never cleared.
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 1024)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            FakeQbittorrent(),
            _DeleteFailsFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            FakeProwlarr([]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="user_reported",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
    assert request is not None
    # The file could not be deleted, so the breadcrumb is kept (not None) even though
    # the status re-armed for the re-search.
    assert request.library_path == str(movie_file)


async def test_report_issue_researches_the_whole_season_after_an_episode_scoped_import(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # The imported download was episode-scoped (episodes=[1]) but the purge removes the
    # whole SEASON directory. The re-search must be season-level (episode=None), else it
    # would refetch only E01 while E02+ (also deleted) stay missing under a "done" season.
    tv_root = tmp_path / "tv"
    season_dir = tv_root / "Some Show" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "Some.Show.S01E01.mkv").write_bytes(b"x" * 2048)

    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399, media_type=MediaType.tv, title="Some Show", status=RequestStatus.available
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=show.id,
                season_number=1,
                status=RequestStatus.available,
                library_path=str(season_dir),
            )
        )
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1399,
                season=1,
                episodes_json=[1],  # episode-SCOPED import
            )
        )
        await session.commit()
        request_id = show.id

    prowlarr = FakeProwlarr([])
    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            FakeQbittorrent(),
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            prowlarr,
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    # The re-search searched the WHOLE season, not just the culprit's episode subset:
    # a single searched request, scoped to season 1 with NO episode narrowing.
    assert len(prowlarr.searched) == 1
    assert prowlarr.searched[0].season == 1
    assert prowlarr.searched[0].episode is None


async def test_report_issue_tv_threads_multi_season_intent_into_research(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tv_root = tmp_path / "tv"
    season_dir = tv_root / "Some Show" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "Some.Show.S01E05.mkv").write_bytes(b"x" * 2048)
    captured: dict[str, object] = {}

    async def capture_preview(*_args: object, **kwargs: object) -> DecisionResult:
        captured["multi_season_intent"] = kwargs["multi_season_intent"]
        captured["episodes"] = kwargs["episodes"]
        return DecisionResult(accepted=(), rejected=(), no_acceptable_release=True)

    monkeypatch.setattr(correction_service.decision_service, "preview", capture_preview)

    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1399,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.available,
            tv_request_mode="explicit_episodes",
            requested_seasons_json=[1, 2],
            requested_episodes_json={"1": [5], "2": [6]},
        )
        session.add(show)
        await session.flush()
        session.add_all(
            [
                SeasonRequest(
                    media_request_id=show.id,
                    season_number=1,
                    status=RequestStatus.available,
                    library_path=str(season_dir),
                ),
                SeasonRequest(
                    media_request_id=show.id,
                    season_number=2,
                    status=RequestStatus.available,
                ),
            ]
        )
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1399,
                season=1,
                episodes_json=[5],
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=1399,
                torrent_hash=_CULPRIT,
                event_type=DownloadHistoryEvent.grabbed,
                source_title="Some.Show.S01E05.1080p.WEB-DL.x264-GROUP",
                indexer="FakeIndexer",
            )
        )
        await session.commit()
        request_id = show.id

    async with sessionmaker_() as session:
        await correction_service.report_issue(
            session,
            FakeQbittorrent(),
            LocalFileSystem(library_roots=[str(tv_root)]),
            FakeLibrary(),
            FakeProwlarr([]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=1,
            roots=LibraryRoots(tv=str(tv_root)),
        )

    intent = captured["multi_season_intent"]
    assert captured["episodes"] is None
    assert isinstance(intent, MultiSeasonRequestIntent)
    assert intent.mode == "explicit_seasons"
    assert intent.requested_seasons == (1, 2)


async def test_cancel_refuses_while_a_download_is_finalizing_its_import(
    sessionmaker_: SessionMaker,
) -> None:
    # A movie whose download is mid-import (`importing`) while the request still reads
    # `downloading`. Cancelling would race the importer's finalize CAS and could strand
    # the placed file under a cancelled request -- refuse, touch nothing.
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="importing",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.ImportInProgressError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(session, qbt, request_id=request_id)

    # Nothing removed, the row is still importing (never flipped to failed), the request
    # is untouched -- the importer is free to finish.
    assert qbt.removed == []
    async with sessionmaker_() as session:
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _CULPRIT))
        ).scalar_one()
        request = await session.get(MediaRequest, request_id)
    assert download.status == "importing"
    assert request is not None and request.status == RequestStatus.downloading


async def test_cancel_refuses_when_an_older_imported_seed_hides_under_a_newer_row(
    sessionmaker_: SessionMaker,
) -> None:
    # An `evicted` season whose NEWEST download row is a later `failed` attempt, over an
    # OLDER `imported` row whose torrent still seeds. Probing only the newest row would
    # miss the imported seed and settle the season cancelled -- orphaning it. The
    # imported-scoped probe catches it and refuses.
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1401,
            media_type=MediaType.tv,
            title="Evicted Show",
            status=RequestStatus.downloading,
        )
        session.add(show)
        await session.flush()
        session.add(
            SeasonRequest(media_request_id=show.id, season_number=1, status=RequestStatus.evicted)
        )
        session.add(
            SeasonRequest(
                media_request_id=show.id, season_number=2, status=RequestStatus.downloading
            )
        )
        # OLD imported seed (lower id) ...
        session.add(
            Download(
                torrent_hash=_ALT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1401,
                season=1,
            )
        )
        # ... hidden under a NEWER failed attempt (higher id) for the SAME season.
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="failed",
                media_request_id=show.id,
                tmdb_id=1401,
                season=1,
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.NotCancellableError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(session, qbt, request_id=request_id)
    assert qbt.removed == []


async def test_cancel_cannot_settle_a_post_eviction_regrab_season_to_cancelled(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #129 regression: the stale-Plex eviction guard (``evicted_seasons``,
    keyed on the newest NON-``cancelled`` row per season) vs. a cancel landing
    AFTER the eviction's finalize while Plex's post-delete scan is still
    pending/failed.

    The disk-truth flip (``eviction_service``'s finalize / recovery: cancelled ->
    evicted) only fires when the in-window same-row re-grab was ALREADY
    'cancelled' at finalize time. If that re-grab is instead still ACTIVE (e.g.
    'downloading') when finalize runs, and the operator cancels it only
    afterward -- while Plex still lists the just-deleted file present -- no code
    path performs that flip for this row. If the cancel could actually settle the
    season to 'cancelled' here, ``evicted_seasons()`` would then see NO
    non-cancelled row left for this season at all (not 'evicted'), so a
    subsequent re-request could mint a stale-Plex 'available' over the deleted
    file -- the exact race the issue describes.

    Nothing needs to change to prevent it: the eviction sweep only ever reclaims
    a season that was previously imported, and eviction never mutates the
    downloads table -- so the season's ORIGINAL imported ``Download`` row always
    still exists underneath the newer re-grab attempt. ``cancel_request``'s
    belt-and-suspenders imported-download probe (``find_latest_imported_for_
    request``, scoped to the imported row specifically, not merely the newest
    attempt) refuses the WHOLE cancel outright on exactly that evidence -- so
    this season can never actually reach 'cancelled' through this path in the
    first place. This test is the permanent guard against ever relaxing that
    probe without addressing this race some other way."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1450,
            media_type=MediaType.tv,
            title="Race Show",
            status=RequestStatus.downloading,
        )
        session.add(show)
        await session.flush()
        # The season the eviction sweep reclaimed, whose same-row re-grab
        # (``season_request_service.ensure_seasons``'s re-arm) is still ACTIVE
        # ('downloading') at finalize -- never 'cancelled', so the finalize's
        # disk-truth flip never had reason to fire for this row.
        session.add(
            SeasonRequest(
                media_request_id=show.id, season_number=1, status=RequestStatus.downloading
            )
        )
        # The ORIGINAL pre-eviction import: eviction deletes the library FILE but
        # never mutates the downloads aggregate, so this row survives the sweep
        # untouched underneath the newer in-window re-grab attempt above.
        session.add(
            Download(
                torrent_hash=_ALT,
                status="imported",
                media_request_id=show.id,
                tmdb_id=1450,
                season=1,
            )
        )
        await session.commit()
        request_id = show.id

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.NotCancellableError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(session, qbt, request_id=request_id)
    assert qbt.removed == []

    # The season truly never reached 'cancelled' -- proving evicted_seasons()'s
    # newest-non-cancelled read is never left without ANY row to subtract for
    # this season, which is exactly what would let a subsequent re-request mint
    # a stale-Plex 'available' over the deleted file.
    async with sessionmaker_() as session:
        season = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
                )
            )
            .scalars()
            .one()
        )
    assert season.status is RequestStatus.downloading


async def test_reset_for_research_resets_autograb_backoff(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """A reported title starts the ADR-0013 backoff ladder over: the culprit's
    accrued search_attempts / next_search_at must not throttle the operator's
    explicit redo (both the movie repo variant and the season variant)."""
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        row.search_attempts = 5
        row.next_search_at = datetime.now(UTC) + timedelta(hours=24)
        season = SeasonRequest(
            media_request_id=request_id,
            season_number=2,
            status=RequestStatus.no_acceptable_release,
            search_attempts=4,
            next_search_at=datetime.now(UTC) + timedelta(hours=12),
        )
        session.add(season)
        await session.commit()

    async with sessionmaker_() as session:
        await SqlRequestRepository(session).reset_for_research(request_id)
        await season_request_service.reset_for_research(
            session, media_request_id=request_id, season_number=2
        )
        await session.commit()

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.searching
        assert row.search_attempts == 0
        assert row.next_search_at is None
        srow = (
            await session.execute(
                select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
            )
        ).scalar_one()
        assert srow.search_attempts == 0
        assert srow.next_search_at is None


# --------------------------------------------------------------------------- #
# Codex round (rebased): PR #32 follow-on findings
# --------------------------------------------------------------------------- #
async def test_cancel_with_no_active_rows_settles_without_a_client(
    sessionmaker_: SessionMaker,
) -> None:
    # Finding #1: a pending/searching cancel with NO active download rows is a pure DB
    # settle -- it never touches qBittorrent -- so it must succeed even with the client
    # UNCONFIGURED (qbt=None), never a spurious service_not_configured.
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.searching,  # not-yet-imported, no download row of its own
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, None, request_id=request_id)
    assert updated.status == RequestStatus.cancelled.value


async def test_cancel_waiting_for_air_date_settles_and_releases_dedup_slot(
    sessionmaker_: SessionMaker,
) -> None:
    """A future TV request is active/dedup-blocking but has no torrent to remove.

    Cancelling it is a pure database settle: both the waiting season and its
    parent become cancelled, and a fresh request for the same show can claim the
    active-media slot.
    """
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=1901,
            media_type=MediaType.tv,
            title="Future Show",
            status=RequestStatus.waiting_for_air_date,
        )
        session.add(request)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=request.id,
                season_number=3,
                status=RequestStatus.waiting_for_air_date,
            )
        )
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, None, request_id=request_id)
    assert updated.status == RequestStatus.cancelled.value

    async with sessionmaker_() as session:
        season = (
            await session.execute(
                select(SeasonRequest).where(SeasonRequest.media_request_id == request_id)
            )
        ).scalar_one()
        assert season.status is RequestStatus.cancelled
        assert await SqlRequestRepository(session).find_active(1901, "tv") is None

        replacement = MediaRequest(
            tmdb_id=1901,
            media_type=MediaType.tv,
            title="Future Show",
            status=RequestStatus.pending,
        )
        session.add(replacement)
        await session.commit()
        assert replacement.id != request_id


async def test_cancel_with_an_active_torrent_requires_a_client(
    sessionmaker_: SessionMaker,
) -> None:
    # Finding #1's honest counterpart: a cancel that owns an ACTIVE torrent genuinely
    # needs the client to remove it. With qbt=None it must refuse UP FRONT
    # (DownloadClientRequiredError -> 409 service_not_configured at the endpoint),
    # never a silent skip that leaks the seeding torrent -- and touch nothing.
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request.id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()
        request_id = request.id

    with pytest.raises(correction_service.DownloadClientRequiredError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(session, None, request_id=request_id)

    # Nothing settled, nothing failed: the guard fired before any mutation.
    async with sessionmaker_() as session:
        request = await session.get(MediaRequest, request_id)
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _CULPRIT))
        ).scalar_one()
    assert request is not None and request.status == RequestStatus.downloading
    assert download.status == "downloading"


async def test_report_issue_refuses_when_a_sibling_claims_the_slot_between_check_and_reset(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding #2: the active-slot claim (re-arm to searching) is now done BEFORE the
    # irreversible torrent-removal/purge. Simulate a racing active sibling grabbing the
    # dedup slot AFTER the upfront find_active check passed but BEFORE the re-arm flush:
    # the flush collides on uq_media_requests_active (a REAL IntegrityError), surfaced
    # as ActiveDuplicateError (409) with NOTHING deleted -- the file untouched, the
    # torrent still present, the blocklist rolled back.
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    settled_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    real_find = SqlDownloadRepository.find_latest_imported_for_request
    inserted = {"done": False}

    async def racing_find(
        self: SqlDownloadRepository, media_request_id: int, *, season: int | None = None
    ) -> object:
        # Runs during culprit resolution -- after the upfront find_active check, before
        # any write of this transaction. Land a NEWER active request for the same media
        # (a committed sibling), so the later re-arm flush collides for real.
        if not inserted["done"]:
            inserted["done"] = True
            async with sessionmaker_() as competitor:
                competitor.add(
                    MediaRequest(
                        tmdb_id=_TMDB,
                        media_type=MediaType.movie,
                        title="Some Movie",
                        year=2020,
                        status=RequestStatus.searching,
                    )
                )
                await competitor.commit()
        return await real_find(self, media_request_id, season=season)

    monkeypatch.setattr(SqlDownloadRepository, "find_latest_imported_for_request", racing_find)

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.ActiveDuplicateError):
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                qbt,
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264", info_hash=_ALT)]),
                GuessitParser(),
                default_profile(),
                request_id=settled_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(root)),
            )

    # The irreversible steps never ran: file on disk, torrent never removed.
    assert movie_file.exists()
    assert qbt.removed == []
    # The blocklist + partial re-arm were rolled back; the reported row is untouched.
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        settled = await session.get(MediaRequest, settled_id)
    assert blocklist == []
    assert settled is not None and settled.status == RequestStatus.available


async def test_report_issue_leaves_scope_searching_when_the_regrab_client_fails(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # Finding #4: the inline re-grab's qbt.add hits a download-client error AFTER the
    # blocklist/purge/reset already committed. It must NOT park no_acceptable_release
    # (a LIE -- releases exist; the CLIENT failed) and must NOT let a 502 escape:
    # the scope is LEFT at 'searching' (the merged auto-grab worker retries it), and the
    # committed blocklist/purge/audit all stand.
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = _AddFailsQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            # An acceptable alternative IS on offer, so the re-grab is attempted and its
            # qbt.add is what fails -- not an empty-preview park.
            FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash=_ALT)]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    # No exception escaped; the scope self-heals at 'searching' (NOT parked, NOT downloading).
    assert updated.status == RequestStatus.searching.value
    # The correction work stands: file purged, culprit torrent removed, blocklist written.
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        downloads = (await session.execute(select(Download))).scalars().all()
    assert len(blocklist) == 1
    # The failed re-grab created no new active download row -- only the terminal culprit.
    assert {d.status for d in downloads} == {"imported"}


async def test_report_issue_leaves_scope_searching_when_the_regrab_leaves_an_untracked_torrent(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # Issue #71: the inline re-grab's qbt.add ACCEPTS the torrent but yields no derivable
    # info-hash (opaque source; the indexer supplied none) -> grab_service raises
    # GrabError, leaving a LIVE, untracked torrent. Mirroring auto-grab's GrabError
    # taxonomy this is OPERATIONAL, NOT content exhaustion: report-issue must NOT park
    # no_acceptable_release (a LIE -- releases exist; the grab PIPELINE failed) and must
    # NOT try another candidate (a second grab against the orphan would double-download).
    # The scope is LEFT at the 'searching' committed at (b) for the auto-grab worker.
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    request_id = await _seed_available_movie(sessionmaker_, library_path=str(movie_file))

    qbt = _EmptyHashQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.report_issue(
            session,
            qbt,
            LocalFileSystem(library_roots=[str(root)]),
            FakeLibrary(),
            # An acceptable alternative IS on offer (info_hash=None models an indexer that
            # supplied no hash), so the re-grab is attempted and its qbt.add is what leaves
            # the untracked torrent -- not an empty-preview park.
            FakeProwlarr([candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER")]),
            GuessitParser(),
            default_profile(),
            request_id=request_id,
            reason="bad_quality",
            season=None,
            roots=LibraryRoots(movies=str(root)),
        )

    # No exception escaped; the scope self-heals at 'searching' (NOT parked, NOT downloading).
    assert updated.status == RequestStatus.searching.value
    # The correction work stands: file purged, culprit torrent removed, blocklist written.
    assert not movie_file.exists()
    assert (_CULPRIT, True) in qbt.removed
    # The re-grab WAS attempted (the untracked torrent is the operational failure), and it
    # was the ONLY grab attempt -- no second candidate was tried against the live orphan.
    assert len(qbt.added) == 1
    async with sessionmaker_() as session:
        blocklist = (await session.execute(select(Blocklist))).scalars().all()
        downloads = (await session.execute(select(Download))).scalars().all()
    assert len(blocklist) == 1
    # The failed re-grab created no new active download row -- only the terminal culprit.
    assert {d.status for d in downloads} == {"imported"}


# --------------------------------------------------------------------------- #
# relocate_stranded_download (issues #133/#157) -- the operator correction verb
# for a torrent whose reported content sits outside every visible /downloads
# mount: relocate it INTO the app's own derived downloads root, then leave it
# retryable for the existing import-retry endpoint.
# --------------------------------------------------------------------------- #
_STRANDED_HASH = "b" * 40


async def _seed_import_blocked_download(
    sm: SessionMaker,
    *,
    torrent_hash: str = _STRANDED_HASH,
    reason: str = PATH_NOT_VISIBLE_REASON_PREFIX
    + "(check volume mounts / content mismatch): /downloads/x",
    status: str = DownloadState.ImportBlocked.value,
) -> int:
    async with sm() as session:
        row = Download(
            torrent_hash=torrent_hash,
            status=status,
            failed_reason=reason,
            tmdb_id=_TMDB,
            year=2020,
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_relocate_stranded_download_requests_the_move_and_stays_retryable(
    sessionmaker_: SessionMaker,
) -> None:
    download_id = await _seed_import_blocked_download(sessionmaker_)
    qbt = FakeQbittorrent()

    async with sessionmaker_() as session:
        updated = await correction_service.relocate_stranded_download(
            session,
            qbt,
            download_id=download_id,
            downloads_host_root="/home/lunchbox/Downloads",
        )

    # The ONLY destination ever handed to qBittorrent is the app's own derived root.
    assert qbt.relocated == [(_STRANDED_HASH, "/home/lunchbox/Downloads")]
    # Left retryable (import_blocked), same state the existing "Retry import"
    # endpoint already resumes from -- but the reason now reflects the relocate.
    assert updated.status == DownloadState.ImportBlocked.value
    assert updated.failed_reason is not None
    assert "/home/lunchbox/Downloads" in updated.failed_reason
    assert "retry the import" in updated.failed_reason


async def test_relocate_stranded_download_rejects_a_missing_download(
    sessionmaker_: SessionMaker,
) -> None:
    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(correction_service.DownloadNotFoundError):
            await correction_service.relocate_stranded_download(
                session,
                qbt,
                download_id=999_999,
                downloads_host_root="/home/lunchbox/Downloads",
            )
    # Nothing was sent to qBittorrent for a download that does not exist.
    assert qbt.relocated == []


async def test_relocate_stranded_download_rejects_a_non_import_blocked_row(
    sessionmaker_: SessionMaker,
) -> None:
    # e.g. still 'downloading' -- nothing stranded to relocate yet.
    download_id = await _seed_import_blocked_download(
        sessionmaker_, status=DownloadState.Downloading.value, reason=""
    )
    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(correction_service.NotRelocatableError):
            await correction_service.relocate_stranded_download(
                session,
                qbt,
                download_id=download_id,
                downloads_host_root="/home/lunchbox/Downloads",
            )
    assert qbt.relocated == []


async def test_relocate_stranded_download_rejects_a_different_import_blocked_reason(
    sessionmaker_: SessionMaker,
) -> None:
    """Scoped EXACTLY to the path-not-visible block -- a DIFFERENT import_blocked
    reason (e.g. a genuinely bad/wrong-media file) has nothing a relocate would
    fix, so it must be refused rather than silently no-op'd."""
    download_id = await _seed_import_blocked_download(
        sessionmaker_, reason="no video file found in the completed torrent"
    )
    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(correction_service.NotRelocatableError):
            await correction_service.relocate_stranded_download(
                session,
                qbt,
                download_id=download_id,
                downloads_host_root="/home/lunchbox/Downloads",
            )
    assert qbt.relocated == []


async def test_relocate_stranded_download_refuses_without_a_derivable_root(
    sessionmaker_: SessionMaker,
) -> None:
    """Root-guard: with no host downloads root derivable (bare metal, no Docker
    split), there is nothing safe to relocate into -- refuse rather than send
    qBittorrent an empty/placeholder location."""
    download_id = await _seed_import_blocked_download(sessionmaker_)
    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        with pytest.raises(correction_service.DownloadsRootUnavailableError):
            await correction_service.relocate_stranded_download(
                session,
                qbt,
                download_id=download_id,
                downloads_host_root="",
            )
    assert qbt.relocated == []


async def test_relocate_stranded_download_propagates_qbittorrent_errors(
    sessionmaker_: SessionMaker,
) -> None:
    """Honesty over silence: a qBittorrent failure during the relocate request
    must surface, never be swallowed into a falsely 'accepted' relocation."""

    class _FailingQbt(FakeQbittorrent):
        async def set_location(self, info_hash: str, save_path: str) -> None:
            raise QbittorrentError("qbittorrent unreachable")

    download_id = await _seed_import_blocked_download(sessionmaker_)
    qbt = _FailingQbt()
    async with sessionmaker_() as session:
        with pytest.raises(QbittorrentError):
            await correction_service.relocate_stranded_download(
                session,
                qbt,
                download_id=download_id,
                downloads_host_root="/home/lunchbox/Downloads",
            )
    # The row is left untouched (still the original path-not-visible reason) --
    # no falsely 'accepted' relocation was recorded.
    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
        assert row is not None
        assert row.status == DownloadState.ImportBlocked.value
        assert row.failed_reason is not None
        assert row.failed_reason.startswith(PATH_NOT_VISIBLE_REASON_PREFIX)


_NEWER_BLOCK_REASON = "no video file found in the completed torrent"


async def test_relocate_stranded_download_surfaces_a_newer_block_reason(
    sessionmaker_: SessionMaker,
) -> None:
    """Round-trip of the CAS fix: a concurrent 'Retry Import' re-blocks the row
    with a NEWER, genuinely different reason in the gap between
    ``relocate_stranded_download`` observing the row and its own terminal write
    landing. The relocate must not clobber that fresher diagnostic with its stale
    "relocation requested" message -- it must surface the newer truth instead."""

    class _ReblockingQbt(FakeQbittorrent):
        """Models a concurrent Retry Import committing a DIFFERENT block reason
        for this same row while ``set_location`` is in flight."""

        async def set_location(self, info_hash: str, save_path: str) -> None:
            await super().set_location(info_hash, save_path)
            async with sessionmaker_() as racer_session:
                row = await racer_session.get(Download, download_id)
                assert row is not None
                row.failed_reason = _NEWER_BLOCK_REASON
                await racer_session.commit()

    download_id = await _seed_import_blocked_download(sessionmaker_)
    qbt = _ReblockingQbt()

    async with sessionmaker_() as session:
        with pytest.raises(correction_service.RelocationSupersededError) as exc_info:
            await correction_service.relocate_stranded_download(
                session,
                qbt,
                download_id=download_id,
                downloads_host_root="/home/lunchbox/Downloads",
            )
    assert exc_info.value.current_reason == _NEWER_BLOCK_REASON

    # The relocation WAS still issued to qBittorrent...
    assert qbt.relocated == [(_STRANDED_HASH, "/home/lunchbox/Downloads")]
    # ...but the row keeps the racer's fresher, genuinely different reason --
    # never overwritten by relocate's stale "relocation requested" text.
    async with sessionmaker_() as session:
        row = await session.get(Download, download_id)
        assert row is not None
        assert row.status == DownloadState.ImportBlocked.value
        assert row.failed_reason == _NEWER_BLOCK_REASON


# --------------------------------------------------------------------------- #
# Issue #206: stale cancel cleanup must not delete a freshly re-owned same-hash
# torrent. ``cancel_request`` now claims each removal as in-flight BEFORE its
# terminal commit and releases it in a ``finally`` once removal settles.
# --------------------------------------------------------------------------- #


class _GrabDuringRemovalQbt(FakeQbittorrent):
    """The #206 regression harness. Cancel's DB commit (A=cancelled, H=terminal
    ``failed``) has already landed by the time ``remove`` is called -- the row
    is externally visible and, absent the removal-in-flight guard, reusable.
    This fake exploits exactly that window: it creates a brand-new request B
    and attempts to grab the SAME hash H for it DURING the removal, before
    ``remove`` itself returns. ``add`` reports ``created=False`` for H,
    mirroring the real client: the torrent is still physically present (this
    very ``remove`` call hasn't returned yet)."""

    def __init__(self, sessionmaker_: SessionMaker, torrent_hash: str, tmdb_id: int) -> None:
        super().__init__(pre_existing={torrent_hash.lower()})
        self._sessionmaker = sessionmaker_
        self._torrent_hash = torrent_hash
        self._tmdb_id = tmdb_id
        self.concurrent_grab_error: Exception | None = None
        self.concurrent_grab_ran = False
        self.new_request_id: int | None = None
        self.new_request_had_no_active_download = False

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        self.concurrent_grab_ran = True
        async with self._sessionmaker() as racer_session:
            new_request = MediaRequest(
                tmdb_id=self._tmdb_id,
                media_type=MediaType.movie,
                title="Some Movie",
                status=RequestStatus.searching,
            )
            racer_session.add(new_request)
            await racer_session.commit()
            self.new_request_id = new_request.id
            try:
                await grab_service.grab(
                    self,
                    racer_session,
                    scored=_scored(self._torrent_hash),
                    request_id=new_request.id,
                    tmdb_id=self._tmdb_id,
                )
            except Exception as exc:  # captured for the test's own assertion, not swallowed
                self.concurrent_grab_error = exc
            active = (
                (
                    await racer_session.execute(
                        select(Download).where(Download.media_request_id == new_request.id)
                    )
                )
                .scalars()
                .all()
            )
            self.new_request_had_no_active_download = active == []
        await super().remove(info_hash, delete_files=delete_files)


async def test_cancel_removal_in_flight_refuses_concurrent_same_hash_reuse(
    sessionmaker_: SessionMaker,
) -> None:
    """The #206 regression: a concurrent same-hash grab for a brand-new request,
    running DURING cancel's torrent removal (after cancel's own DB commit
    already landed), must be refused rather than re-own the row -- and the
    stale cancellation must go on to remove the ORIGINAL owner's torrent
    exactly as before, leaving the row terminal and still owned by A."""
    async with sessionmaker_() as session:
        a = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(a)
        await session.flush()
        a_id = a.id
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=a_id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        download_id = (
            (await session.execute(select(Download).where(Download.torrent_hash == _CULPRIT)))
            .scalar_one()
            .id
        )

    qbt = _GrabDuringRemovalQbt(sessionmaker_, _CULPRIT, _TMDB)
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=a_id)

    assert updated.status == RequestStatus.cancelled.value
    assert qbt.concurrent_grab_ran
    # The concurrent grab for B was refused with the #206 error, not a silent reuse.
    assert isinstance(qbt.concurrent_grab_error, TorrentRemovalInFlightError)
    assert qbt.concurrent_grab_error.torrent_hash == _CULPRIT.lower()
    assert qbt.concurrent_grab_error.download_id == download_id
    # B never got a download row attached to it -- the guard fired before any write.
    assert qbt.new_request_had_no_active_download
    # The stale cancellation still removed the torrent -- it must go on to do
    # what it always did, exactly as before #206.
    assert (_CULPRIT, True) in qbt.removed

    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        b = await session.get(MediaRequest, qbt.new_request_id)
    assert download is not None
    assert download.status == "failed"  # still terminal
    assert download.media_request_id == a_id  # NEVER repointed to B
    assert b is not None and b.status == RequestStatus.searching  # untouched, no download

    # The finally released the claim once removal settled.
    assert not queue_service.removal_in_flight(download_id)


async def test_cancel_with_grab_before_terminal_is_refused_by_active_guard(
    sessionmaker_: SessionMaker,
) -> None:
    """The other ordering: while A still owns H as an ACTIVE (downloading) row
    -- BEFORE cancel ever runs -- a grab for a different request B of the SAME
    hash cannot possibly reuse it. The known-hash precheck refuses NON-terminal
    ownership outright (``TorrentAlreadyTrackedError``), no CAS or registry
    involved at all. Reuse only becomes POSSIBLE once cancel's own commit makes
    the row terminal -- exactly the window the #206 fix closes. Documents that
    the dangerous "grab wins entirely before cancel" ordering cannot occur:
    terminality is a precondition for reuse, and only cancel's commit produces
    it."""
    async with sessionmaker_() as session:
        a = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        b = MediaRequest(
            tmdb_id=9999,
            media_type=MediaType.movie,
            title="Some Other Movie",
            status=RequestStatus.searching,
        )
        session.add_all([a, b])
        await session.flush()
        a_id, b_id = a.id, b.id
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=a_id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        with pytest.raises(TorrentAlreadyTrackedError) as excinfo:
            await grab_service.grab(
                FakeQbittorrent(),
                session,
                scored=_scored(_CULPRIT),
                request_id=b_id,
                tmdb_id=9999,
            )
    assert excinfo.value.owner_request_id == a_id

    # A's download is untouched by the refused grab attempt -- cancel proceeds
    # normally, exactly as if the grab attempt never happened.
    qbt = FakeQbittorrent()
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=a_id)
    assert updated.status == RequestStatus.cancelled.value
    assert (_CULPRIT, True) in qbt.removed
    async with sessionmaker_() as session:
        download = (
            await session.execute(select(Download).where(Download.torrent_hash == _CULPRIT))
        ).scalar_one()
    assert download.status == "failed"
    assert download.media_request_id == a_id


async def test_cancel_removal_failure_leaves_honest_state_and_releases_claim(
    sessionmaker_: SessionMaker,
) -> None:
    """A genuine removal FAILURE (the client errors) must still leave cancel
    honest: the request settles cancelled, the row goes terminal, and -- the
    #206 finally-correctness guarantee -- the in-flight claim is released even
    though the removal itself never succeeded, so a later grab of the same hash
    is not permanently locked out."""

    class _RemoveFailsQbt(FakeQbittorrent):
        async def remove(self, info_hash: str, *, delete_files: bool) -> None:
            raise QbittorrentError("qbittorrent unreachable")

    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request_id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        download_id = (
            (await session.execute(select(Download).where(Download.torrent_hash == _CULPRIT)))
            .scalar_one()
            .id
        )

    qbt = _RemoveFailsQbt()
    async with sessionmaker_() as session:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)

    # Best-effort: the removal failure never aborts the correction.
    assert updated.status == RequestStatus.cancelled.value
    async with sessionmaker_() as session:
        download = await session.get(Download, download_id)
        request = await session.get(MediaRequest, request_id)
    assert download is not None and download.status == "failed"
    assert request is not None and request.status == RequestStatus.cancelled

    # The finally released the claim even though removal FAILED -- a stuck
    # claim would permanently lock a legitimate future re-grab out.
    assert not queue_service.removal_in_flight(download_id)

    # Proof it's genuinely released, not just unobserved: a fresh grab of the
    # same hash for a new request now reuses cleanly (the data is intact --
    # removal never actually happened).
    new_request = MediaRequest(
        tmdb_id=_TMDB,
        media_type=MediaType.movie,
        title="Some Movie",
        status=RequestStatus.searching,
    )
    async with sessionmaker_() as session:
        session.add(new_request)
        await session.commit()
        new_request_id = new_request.id

    async with sessionmaker_() as session:
        record = await grab_service.grab(
            FakeQbittorrent(),
            session,
            scored=_scored(_CULPRIT),
            request_id=new_request_id,
            tmdb_id=_TMDB,
        )
    assert record.status == "downloading"
    assert record.media_request_id == new_request_id


async def test_cancel_releases_in_flight_claim_on_import_race_abort(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finally-correctness: request A has TWO active downloads (two seasons
    downloading at once). The first row's CAS succeeds and registers its
    in-flight claim; the second row races out of the cancellable set (a
    concurrent import finalize claiming it ``importing``) between cancel's
    snapshot and its own CAS, so the whole cancel rolls back and raises
    ``ImportInProgressError``. The already-registered first claim must not
    leak -- the ``finally`` releases it even on this abort path."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=1500,
            media_type=MediaType.tv,
            title="Racing Show",
            status=RequestStatus.downloading,
        )
        session.add(show)
        await session.flush()
        show_id = show.id
        first = Download(
            torrent_hash="c" * 40,
            status="downloading",
            media_request_id=show_id,
            tmdb_id=1500,
            season=1,
        )
        second = Download(
            torrent_hash="d" * 40,
            status="downloading",
            media_request_id=show_id,
            tmdb_id=1500,
            season=2,
        )
        session.add_all([first, second])
        await session.commit()
        first_id, second_id = first.id, second.id

    real_update = correction_service.SqlDownloadRepository.update_status_if_in

    async def racing_update(
        self: SqlDownloadRepository,
        download_id: int,
        status: str,
        allowed_from: frozenset[str],
        **kwargs: Any,
    ) -> bool:
        if download_id == second_id:
            # Simulate the row racing out of the cancellable set (e.g. a
            # concurrent import finalize claiming it 'importing') between
            # cancel's snapshot and its own CAS -- no real second DB writer is
            # needed to prove the point: the CAS predicate simply loses.
            return False
        return await real_update(self, download_id, status, allowed_from, **kwargs)

    monkeypatch.setattr(
        correction_service.SqlDownloadRepository, "update_status_if_in", racing_update
    )

    qbt = FakeQbittorrent()
    with pytest.raises(correction_service.ImportInProgressError):
        async with sessionmaker_() as session:
            await correction_service.cancel_request(session, qbt, request_id=show_id)

    # The first row's claim WAS registered before the abort -- proving the
    # finally released it, not merely that nothing was ever registered.
    assert not queue_service.removal_in_flight(first_id)
    assert not queue_service.removal_in_flight(second_id)
    # Nothing was removed (the whole cancel rolled back) and nothing settled.
    assert qbt.removed == []
    async with sessionmaker_() as session:
        show_row = await session.get(MediaRequest, show_id)
        first_row = await session.get(Download, first_id)
        second_row = await session.get(Download, second_id)
    assert show_row is not None and show_row.status == RequestStatus.downloading
    assert first_row is not None and first_row.status == "downloading"  # rolled back
    assert second_row is not None and second_row.status == "downloading"  # never touched
