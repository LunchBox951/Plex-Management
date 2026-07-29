"""Durable add-intent repository contract."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.repositories import CreateDownloadAddIntent, DownloadAddIntentScopeCreate
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository


async def test_create_normalizes_hash_and_persists_target_and_ride_along_scopes(
    session: AsyncSession,
) -> None:
    repo = SqlDownloadAddIntentRepository(session)

    intent = await repo.create(
        CreateDownloadAddIntent(
            torrent_hash="ABCDEF",
            source="magnet:?xt=urn:btih:abcdef",
            tmdb_id=7,
            media_type="tv",
            save_path="/downloads",
            scopes=(
                DownloadAddIntentScopeCreate(
                    tmdb_id=7,
                    media_type="tv",
                    scope_key="season:1",
                    season_number=1,
                    episodes=(2, 1, 2),
                    is_target=True,
                ),
                DownloadAddIntentScopeCreate(
                    tmdb_id=7,
                    media_type="tv",
                    scope_key="season:2",
                    season_number=2,
                    is_target=False,
                ),
            ),
        )
    )

    assert intent.torrent_hash == "abcdef"
    assert intent.source == "magnet:?xt=urn:btih:abcdef"
    assert [(scope.scope_key, scope.episodes, scope.is_target) for scope in intent.scopes] == [
        ("season:1", (1, 2), True),
        ("season:2", None, False),
    ]


async def test_same_hash_is_idempotent_but_title_scope_collision_is_not(
    session: AsyncSession,
) -> None:
    repo = SqlDownloadAddIntentRepository(session)
    command = CreateDownloadAddIntent(
        torrent_hash="one",
        tmdb_id=7,
        media_type="movie",
        save_path="",
        scopes=(DownloadAddIntentScopeCreate(tmdb_id=7, media_type="movie", scope_key="movie"),),
    )
    first = await repo.create(command)
    assert (await repo.create(command)).id == first.id

    owner = await repo.create(
        CreateDownloadAddIntent(
            torrent_hash="two",
            tmdb_id=7,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=7, media_type="movie", scope_key="movie"),
            ),
        )
    )
    assert owner.id == first.id
    assert owner.torrent_hash == first.torrent_hash


async def test_state_cas_and_delete_cascades_scopes(session: AsyncSession) -> None:
    repo = SqlDownloadAddIntentRepository(session)
    intent = await repo.create(
        CreateDownloadAddIntent(
            torrent_hash="one",
            tmdb_id=7,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=7, media_type="movie", scope_key="movie"),
            ),
        )
    )
    assert await repo.mark_state(intent.id, "cancel_requested", expected_state="prepared")
    assert not await repo.mark_state(intent.id, "needs_attention", expected_state="prepared")
    deleted = await repo.delete(intent.id)
    assert deleted


async def test_needs_attention_intent_is_parked_outside_recovery_queue(
    session: AsyncSession,
) -> None:
    repo = SqlDownloadAddIntentRepository(session)
    intent = await repo.create(
        CreateDownloadAddIntent(
            torrent_hash="parked",
            tmdb_id=7,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=7, media_type="movie", scope_key="movie"),
            ),
        )
    )
    assert await repo.mark_state(intent.id, "needs_attention", expected_state="prepared")

    assert await repo.list_recoverable() == []


async def test_season_cas_ignores_active_download_for_other_show_same_season(
    session: AsyncSession,
) -> None:
    first = MediaRequest(
        tmdb_id=70, media_type=MediaType.tv, title="First", status=RequestStatus.pending
    )
    second = MediaRequest(
        tmdb_id=71, media_type=MediaType.tv, title="Second", status=RequestStatus.pending
    )
    session.add_all((first, second))
    await session.flush()
    season = SeasonRequest(
        media_request_id=second.id, season_number=2, status=RequestStatus.pending
    )
    session.add(season)
    session.add(
        Download(
            torrent_hash="first-active",
            status="downloading",
            media_request_id=first.id,
            tmdb_id=first.tmdb_id,
            media_type=MediaType.tv,
            season=2,
        )
    )
    await session.flush()

    assert await SqlSeasonRequestRepository(session).set_status_if_in(
        season.id,
        RequestStatus.no_acceptable_release.value,
        frozenset({RequestStatus.pending.value}),
        require_no_active_download_or_intent=True,
    )


async def test_movie_and_season_cas_refuse_matching_future_intent(session: AsyncSession) -> None:
    movie = MediaRequest(
        tmdb_id=70, media_type=MediaType.movie, title="Movie", status=RequestStatus.pending
    )
    show = MediaRequest(
        tmdb_id=71,
        media_type=MediaType.tv,
        title="Show",
        status=RequestStatus.pending,
    )
    session.add_all((movie, show))
    await session.flush()
    season = SeasonRequest(media_request_id=show.id, season_number=2, status=RequestStatus.pending)
    session.add(season)
    await session.flush()
    await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="movie-intent",
            tmdb_id=70,
            media_type="movie",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(tmdb_id=70, media_type="movie", scope_key="movie"),
            ),
        )
    )
    await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
            torrent_hash="show-intent",
            tmdb_id=71,
            media_type="tv",
            save_path="",
            scopes=(
                DownloadAddIntentScopeCreate(
                    tmdb_id=71, media_type="tv", scope_key="season:2", season_number=2
                ),
            ),
        )
    )
    assert not await SqlRequestRepository(session).set_status_if_in(
        movie.id,
        RequestStatus.no_acceptable_release.value,
        frozenset({RequestStatus.pending.value}),
        require_no_active_download_or_intent=True,
    )
    assert not await SqlSeasonRequestRepository(session).set_status_if_in(
        season.id,
        RequestStatus.no_acceptable_release.value,
        frozenset({RequestStatus.pending.value}),
        require_no_active_download_or_intent=True,
    )
