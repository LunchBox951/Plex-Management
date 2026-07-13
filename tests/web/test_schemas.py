"""Issue #205 — lifecycle ``status`` fields fail closed at the wire boundary.

An unrecognized status string must raise ``ValidationError`` when building the
response DTO, and every canonical enum member must construct cleanly. This is
the fail-closed contract the OpenAPI enum typing (see
``test_openapi_status_enums.py``) exists to advertise.
"""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import DownloadScopeStatus, RequestStatus
from plex_manager.ports.repositories import DownloadRecord, DownloadScopeRecord
from plex_manager.web.routers.queue import _to_item  # pyright: ignore[reportPrivateUsage]
from plex_manager.web.schemas import QueueItem, QueueScope, RequestResponse, SeasonStatus


def test_request_response_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        RequestResponse(
            id=1,
            tmdb_id=1,
            media_type="movie",
            title="x",
            # A real unknown value only ever arrives as a plain str crossing the
            # wire/DB boundary (never as a `RequestStatus` pyright would accept
            # unchallenged) -- `cast` documents that boundary for the type
            # checker without changing the runtime str pydantic must reject.
            status=cast(RequestStatus, "bogus"),
        )


@pytest.mark.parametrize("member", list(RequestStatus))
def test_request_response_accepts_every_request_status(member: RequestStatus) -> None:
    response = RequestResponse(
        id=1,
        tmdb_id=1,
        media_type="movie",
        title="x",
        # `.value` -- a plain str, exactly what a service layer/DB row hands the
        # DTO constructor -- exercises pydantic's str -> enum coercion, not mere
        # enum-identity passthrough; `cast` documents the boundary for pyright.
        status=cast(RequestStatus, member.value),
    )
    assert response.status == member


def test_season_status_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SeasonStatus(season_number=1, status=cast(RequestStatus, "bogus"))


@pytest.mark.parametrize("member", list(RequestStatus))
def test_season_status_accepts_every_request_status(member: RequestStatus) -> None:
    season = SeasonStatus(season_number=1, status=cast(RequestStatus, member.value))
    assert season.status == member


def test_queue_item_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        QueueItem(id=1, torrent_hash="abc", status=cast(DownloadState, "bogus"))


@pytest.mark.parametrize("member", list(DownloadState))
def test_queue_item_accepts_every_download_state(member: DownloadState) -> None:
    item = QueueItem(id=1, torrent_hash="abc", status=cast(DownloadState, member.value))
    assert item.status == member


def test_queue_scope_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        QueueScope(status=cast(DownloadScopeStatus, "bogus"))


@pytest.mark.parametrize("member", list(DownloadScopeStatus))
def test_queue_scope_accepts_every_scope_status(member: DownloadScopeStatus) -> None:
    scope = QueueScope(status=cast(DownloadScopeStatus, member.value))
    assert scope.status == member


def test_queue_scope_default_status_is_active() -> None:
    scope = QueueScope()
    assert scope.status == DownloadScopeStatus.active


def test_to_item_round_trips_download_state_and_scope_status() -> None:
    """``_to_item`` maps a repository ``DownloadRecord`` (plain ``str`` status,
    per the P2/P4 decoupling in ``repositories/downloads.py``) onto the wire
    ``QueueItem`` — the real ``DownloadState``/``DownloadScopeStatus`` values a
    service layer writes must serialize to the expected string unchanged."""
    record = DownloadRecord(
        id=7,
        torrent_hash="deadbeef",
        status=DownloadState.ImportBlocked.value,
        scopes=(
            DownloadScopeRecord(
                id=1,
                download_id=7,
                status=DownloadScopeStatus.import_blocked.value,
            ),
        ),
    )
    item = _to_item(record)
    assert item.status is DownloadState.ImportBlocked
    assert item.model_dump()["status"] == "import_blocked"
    assert item.scopes[0].status is DownloadScopeStatus.import_blocked
    assert item.model_dump()["scopes"][0]["status"] == "import_blocked"
