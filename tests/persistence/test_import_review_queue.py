"""Only requests that can accept an override retain actionable review cards."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus
from plex_manager.repositories.downloads import SqlDownloadRepository


@pytest.mark.parametrize(
    "status, visible",
    [
        (RequestStatus.searching, True),
        (RequestStatus.downloading, True),
        (RequestStatus.import_blocked, True),
        (RequestStatus.completed, False),
        (RequestStatus.available, False),
        (RequestStatus.cancelled, False),
    ],
)
async def test_settled_review_holds_leave_the_queue_without_discarding_the_record(
    session: AsyncSession,
    status: RequestStatus,
    visible: bool,
) -> None:
    request = MediaRequest(
        tmdb_id=603, media_type=MediaType.movie, title="Some Movie", status=status
    )
    session.add(request)
    await session.flush()
    held = Download(
        torrent_hash="held",
        media_request_id=request.id,
        tmdb_id=603,
        status="no_acceptable_release",
        import_review_reason="runtime conflict",
    )
    session.add(held)
    await session.commit()

    queue = await SqlDownloadRepository(session).list_active_for_queue()

    assert [row.id for row in queue] == ([held.id] if visible else [])
    persisted = (await session.execute(select(Download).where(Download.id == held.id))).scalar_one()
    assert persisted.status == "no_acceptable_release"
    assert persisted.import_review_reason == "runtime conflict"
