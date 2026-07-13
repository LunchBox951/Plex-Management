"""SQLAlchemy implementations of the repository ports.

The domain depends only on the ``ports.repositories`` Protocols; the concrete
classes here adapt :class:`~sqlalchemy.ext.asyncio.AsyncSession` row access to and
from the frozen read-model DTOs (``RequestRecord`` / ``DownloadRecord`` /
``BlocklistRecord``). Each repository is constructed with the session it should
use and flushes (never commits) so it composes inside a caller-owned unit of
work.
"""

from __future__ import annotations

from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.repositories.update_coordination import SqlUpdateCoordinationRepository

__all__ = [
    "SqlBlocklistRepository",
    "SqlDownloadRepository",
    "SqlLogEventRepository",
    "SqlRequestRepository",
    "SqlSeasonEpisodeStateRepository",
    "SqlSeasonRequestRepository",
    "SqlUpdateCoordinationRepository",
]
